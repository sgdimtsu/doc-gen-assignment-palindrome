"""Code-owned record enrichment and simple per-run output files."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from agent_pipeline import ROOT
from agent_pipeline.extraction.models import ExtractionRun, ExtractionSettings
from agent_pipeline.extraction.structured import merge_candidate
from agent_pipeline.validation.status import (
    candidate_errors,
    execution_errors,
    validation_state,
)


class ExtractionRecords:
    def __init__(
        self, schema_path=ROOT / "config/extraction_schema.json", structured=None
    ):
        self.structured = structured or {}
        self.response_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.response_validator = Draft202012Validator(self.response_schema)
        self.result_validator = Draft202012Validator(
            self._result_schema(self.response_schema)
        )

    @staticmethod
    def _result_schema(response_schema):
        schema = deepcopy(response_schema)
        definitions = schema["$defs"]
        source = definitions["source"]
        source["properties"].update(
            {
                "filename": {"type": "string"},
                "format": {"type": "string"},
                "read_status": {
                    "enum": ["pending", "read", "partial", "failed", "unsupported"]
                },
            }
        )
        source["required"] += ["filename", "format", "read_status"]
        for name, definition in definitions.items():
            if name.endswith("_observation"):
                definition["properties"].update(
                    {
                        "observation_id": {"type": "string"},
                    }
                )
                definition["required"] += ["observation_id"]
        issue = definitions["extraction_issue"]
        del issue["properties"]["observation_indexes"]
        issue["required"].remove("observation_indexes")
        issue["properties"].update(
            {
                "issue_id": {"type": "string"},
                "observation_ids": {"type": "array", "items": {"type": "string"}},
            }
        )
        issue["required"] += ["issue_id", "observation_ids"]
        schema["properties"]["request_fields"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["source_id", "label", "value"],
                "additionalProperties": False,
            },
        }
        return schema

    def assemble(self, candidate: dict, inventory: list, run_id: str) -> dict:
        self.response_validator.validate(candidate)
        response = (
            merge_candidate(candidate, **self.structured)
            if self.structured
            else deepcopy(candidate)
        )
        problems = candidate_errors(candidate, inventory)
        if problems:
            raise ValueError("; ".join(problems))
        inventory = {source["source_id"]: source for source in inventory}
        classifications = {
            source["source_id"]: source for source in response["sources"]
        }
        sources = []
        for sid, metadata in inventory.items():
            classification = classifications[sid]
            sources.append(
                {
                    **classification,
                    **{
                        key: metadata[key]
                        for key in ("source_id", "filename", "format", "read_status")
                    },
                }
            )
        observations = response["observations"]
        for index, observation in enumerate(observations):
            observation["observation_id"] = f"{run_id}:observation-{index + 1:04d}"
        issues = response["extraction_issues"]
        for index, issue in enumerate(issues):
            issue_id = f"{run_id}:issue-{index + 1:04d}"
            indexes = issue.pop("observation_indexes")
            issue["issue_id"] = issue_id
            issue["observation_ids"] = [
                observations[i]["observation_id"] for i in indexes
            ]
        result = {
            "sources": sources,
            "observations": observations,
            "extraction_issues": issues,
        }
        if self.structured:
            result["request_fields"] = deepcopy(self.structured["request_fields"])
        self.result_validator.validate(result)
        return result


def write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def save_run(
    output_dir: Path, client_name: str, run: ExtractionRun, settings: ExtractionSettings
) -> Path:
    if (
        not client_name
        or Path(client_name).name != client_name
        or client_name in {".", ".."}
        or "\\" in client_name
    ):
        raise ValueError("Client name must be a single folder name")
    root = output_dir.resolve()
    folder = (root / client_name / run.run_id).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("Client output resolves outside the output directory")
    folder.mkdir(parents=True, exist_ok=False)
    result = None
    if run.response is not None:
        try:
            result = ExtractionRecords(structured=run.structured).assemble(
                run.response, run.inventory, run.run_id
            )
        except (ValueError, KeyError, TypeError, ValidationError) as error:
            run.errors.append("Cannot assemble extraction: " + type(error).__name__)
    problems = execution_errors(run.inventory)
    run.errors.extend(error for error in problems if error not in run.errors)
    run.state = (
        "complete"
        if run.state == "complete" and not run.errors and result is not None
        else "partial"
    )
    if result is not None:
        write_json(
            folder
            / (
                "extraction.json"
                if run.state == "complete"
                else "partial_extraction.json"
            ),
            result,
        )
    elif run.response is not None or run.last_output_text is not None:
        write_json(
            folder / "model_response.json",
            {"response": run.response, "last_output_text": run.last_output_text},
        )
    write_json(folder / "validation.json", run.validation_reports)
    write_json(
        folder / "summary.json",
        {
            "run_id": run.run_id,
            "client": client_name,
            "state": run.state,
            "validation_state": validation_state(
                run.response, run.validation_reports, result
            ),
            "errors": run.errors,
            "tool_errors": run.tool_errors,
            "inventory": run.inventory,
            "settings": asdict(settings),
            "model_calls": run.model_calls,
            "tool_calls": run.tool_calls,
            "usage": run.usage,
            "elapsed_seconds": run.elapsed_seconds,
            "final_checks": run.final_checks,
            "validation_attempts": len(run.validation_reports),
            "validation_model_calls": sum(
                r.get("model_calls", 0) for r in run.validation_reports
            ),
            "validation_usage": {
                key: sum(r.get("usage", {}).get(key, 0) for r in run.validation_reports)
                for key in ("input_tokens", "output_tokens")
            },
        },
    )
    return folder
