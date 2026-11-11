"""Tool-using extraction, executed by the OpenAI Agents SDK."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import time
from typing import Any

from agents import (
    Agent,
    AgentOutputSchemaBase,
    FunctionTool,
    ModelBehaviorError,
    ModelSettings,
    OpenAIChatCompletionsModel,
    RunConfig,
    RunHooks,
    Runner,
)
from jsonschema import Draft202012Validator, ValidationError

from agent_pipeline import ROOT
from agent_pipeline.extraction.sources import SourceTools
from agent_pipeline.extraction.tools import TOOL_DEFINITIONS
from agent_pipeline.extraction.models import ExtractionRun, ExtractionSettings
from agent_pipeline.validation.tool import CandidateValidationTool
from agent_pipeline.validation.status import execution_errors
from agent_pipeline.validation.checks import check_extraction


class CheckedChatCompletionsModel(OpenAIChatCompletionsModel):
    # The SDK accepts parseable JSON even when finish_reason is "length".
    # Keep our stricter completion gate at this Chat Completions boundary.
    async def _fetch_response(self, *args, **kwargs):
        response = await super()._fetch_response(*args, **kwargs)
        if response.choices and response.choices[0].finish_reason not in {
            "stop",
            "tool_calls",
        }:
            raise ModelBehaviorError("Model response did not finish")
        return response


class ExtractionOutput(AgentOutputSchemaBase):
    def __init__(self, schema):
        self.schema = schema
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema)

    def is_plain_text(self):
        return False

    def name(self):
        return "client_extraction"

    def json_schema(self):
        return self.schema

    def is_strict_json_schema(self):
        return True

    def validate_json(self, json_str):
        try:
            data = json.loads(json_str)
            self.validator.validate(data)
            return data
        except (ValueError, ValidationError) as error:
            raise ModelBehaviorError(
                "Invalid extraction JSON: " + type(error).__name__
            ) from None


class ExtractionHooks(RunHooks):
    def __init__(self, run, images):
        self.run = run
        self.images = images

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        self.run.model_calls += 1
        # Chat Completions cannot receive images inside tool-result messages.
        input_items.extend(image for image in self.images if image not in input_items)

    async def on_llm_end(self, context, agent, response):
        text = "".join(
            part.text
            for item in response.output
            if item.type == "message"
            for part in item.content
            if part.type == "output_text"
        )
        if text:
            self.run.last_output_text = text
        usage = context.usage
        self.run.usage = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }


class ExtractionAgent:
    def __init__(
        self,
        client: Any,
        settings: ExtractionSettings,
        instructions_path: Path = ROOT / "EXTRACTION_SCHEMA.md",
        schema_path: Path = ROOT / "config/extraction_schema.json",
    ):
        self.client = client.with_options(
            max_retries=settings.api_retries, timeout=settings.request_timeout_seconds
        )
        self.settings = settings
        self.instructions = instructions_path.read_text(encoding="utf-8")
        self.schema_path = schema_path
        self.output = ExtractionOutput(
            json.loads(schema_path.read_text(encoding="utf-8"))
        )
        self.validator = self.output.validator

    async def run(self, client_dir: Path) -> ExtractionRun:
        started = time.monotonic()
        run = ExtractionRun()
        sources = SourceTools(client_dir)
        images = []
        listed = False
        delivered = set()
        consecutive_errors = 0
        validation_tool = CandidateValidationTool(
            self.client,
            self.settings,
            self.schema_path,
            sources,
            run,
            delivered,
        )

        def make_tool(definition):
            async def invoke(context, arguments):
                nonlocal listed, consecutive_errors
                if run.tool_calls >= self.settings.max_tool_calls:
                    run.errors.append("Tool-call limit reached")
                    raise RuntimeError("Tool-call limit reached")
                run.tool_calls += 1
                try:
                    args = json.loads(arguments)
                    Draft202012Validator(definition["parameters"]).validate(args)
                    if definition["name"] != "list_files" and not listed:
                        raise ValueError("Call list_files before reading sources")
                    result = getattr(sources, definition["name"])(**args)
                    content = dict(result)
                    image = content.pop("image", None)
                    text = json.dumps(content, ensure_ascii=False, allow_nan=False)
                    size = len(text.encode("utf-8"))
                    if image:
                        size += len(image["image_url"].encode("utf-8"))
                    if size > self.settings.max_tool_output_bytes:
                        raise ValueError("Tool output exceeds configured size limit")
                    if not result.get("ok", definition["name"] == "list_files"):
                        consecutive_errors += 1
                        if (
                            consecutive_errors
                            >= self.settings.max_consecutive_tool_errors
                        ):
                            raise RuntimeError("Repeated source-reading errors")
                        return text
                    if image:
                        images.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "Image evidence for "
                                        + args["source_id"],
                                    },
                                    image,
                                ],
                            }
                        )
                    if definition["name"] == "list_files":
                        listed = True
                    else:
                        delivered.add(args["source_id"])
                    consecutive_errors = 0
                    return text
                except Exception as error:
                    consecutive_errors += 1
                    run.tool_errors.append(
                        {"tool": definition["name"], "error": type(error).__name__}
                    )
                    if consecutive_errors >= self.settings.max_consecutive_tool_errors:
                        raise RuntimeError(
                            "Repeated invalid or failed tool requests"
                        ) from None
                    return json.dumps(
                        {
                            "ok": False,
                            "error": type(error).__name__,
                            "message": "Check arguments, inventory, read order and output-size limit.",
                        }
                    )

            return FunctionTool(
                name=definition["name"],
                description=definition["description"],
                params_json_schema=definition["parameters"],
                on_invoke_tool=invoke,
            )

        try:
            missing = sources.list_files()["missing_required_files"]
            if missing:
                run.errors.append("Missing required files: " + ", ".join(missing))
                return run
            agent = Agent(
                name="Evidence extractor",
                instructions=self.instructions,
                model=CheckedChatCompletionsModel(
                    model=self.settings.model, openai_client=self.client
                ),
                tools=[make_tool(d) for d in TOOL_DEFINITIONS]
                + [validation_tool.definition()],
                output_type=self.output,
                model_settings=ModelSettings(
                    max_tokens=self.settings.max_output_tokens,
                    parallel_tool_calls=False,
                    store=False,
                ),
            )
            result = await Runner.run(
                agent,
                "Call list_files, read both relevance files, then read all remaining sources. "
                "Prepare a candidate and call validate_extraction. Repair reported errors within "
                "the validation budget. When attempts are exhausted, return the best candidate with "
                "uncertainty preserved. Validation findings are advisory, not a release gate; "
                "do not reconcile or write a report.",
                max_turns=self.settings.max_model_calls,
                hooks=ExtractionHooks(run, images),
                run_config=RunConfig(tracing_disabled=True),
            )
            run.response = result.final_output
            run.last_output_text = json.dumps(run.response, ensure_ascii=False)
            if not listed:
                run.errors.append("File inventory was not requested")
            if set(sources.sources) - delivered:
                run.errors.append(
                    "Sources not delivered: "
                    + ", ".join(sorted(set(sources.sources) - delivered))
                )
            run.errors.extend(execution_errors(sources.list_files()["sources"]))
            if not run.errors:
                run.state = "complete"
        except Exception as error:
            run.errors.append("Extraction stopped: " + type(error).__name__)
            if getattr(error, "status_code", None) is not None:
                run.errors.append("API HTTP status: " + str(error.status_code))
        finally:
            run.inventory = sources.list_files()["sources"]
            if run.response is not None:
                try:
                    final = validation_tool.store.assemble(
                        run.response, run.inventory, run.run_id
                    )
                    evidence = validation_tool.reviewer._load_evidence(
                        client_dir, final
                    )
                    run.final_checks = {
                        "findings": check_extraction(
                            final, evidence, validation_tool.store.result_validator
                        ),
                        "errors": [],
                    }
                except Exception as error:
                    run.final_checks = {
                        "findings": [],
                        "errors": [
                            "Final deterministic checks could not complete: "
                            + type(error).__name__
                        ],
                    }
            run.elapsed_seconds = round(time.monotonic() - started, 3)
        return run
