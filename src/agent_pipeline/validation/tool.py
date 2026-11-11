"""Bounded advisory review of extractor candidates."""

from copy import deepcopy
import json

from agents import FunctionTool
from jsonschema import Draft202012Validator, ValidationError

from agent_pipeline.extraction.results import ExtractionRecords
from agent_pipeline.validation.review import ExtractionValidator, ValidationSettings
from agent_pipeline.validation.status import (
    candidate_hash,
    candidate_errors,
)
from agent_pipeline.extraction.tools import validation_definition


class CandidateValidationTool:
    def __init__(
        self,
        client,
        settings,
        schema_path,
        sources,
        run,
        delivered,
    ):
        self.client = client
        self.settings = settings
        self.sources = sources
        self.run = run
        self.delivered = delivered
        # Assembly only adds code-owned metadata and IDs; it writes no files.
        self.store = ExtractionRecords(schema_path=schema_path)
        self.reviewer = ExtractionValidator(
            ValidationSettings(
                model=settings.validation_model or settings.model,
                max_output_tokens=settings.validation_max_output_tokens,
                timeout_seconds=settings.request_timeout_seconds,
                api_retries=settings.api_retries,
                max_evidence_bytes=settings.max_tool_output_bytes,
            ),
            extraction_schema_path=schema_path,
        )

    def structured_evidence(self):
        return dict(
            protected=[
                o for records in self.sources.protected.values() for o in records
            ],
            roles=self.sources.roles,
            request_fields=self.sources.request_fields,
        )

    def definition(self):
        definition = validation_definition(self.store.response_schema)
        self.parameters = definition["parameters"]
        return FunctionTool(
            name=definition["name"],
            description=definition["description"],
            params_json_schema=self.parameters,
            on_invoke_tool=self.invoke,
            is_enabled=lambda context, agent: self.sources_ready(),
        )

    def sources_ready(self):
        return set(self.sources.sources) <= self.delivered and all(
            source["read_status"] == "read" for source in self.sources.sources.values()
        )

    async def invoke(self, context, arguments):
        if self.run.tool_calls >= self.settings.max_tool_calls:
            raise RuntimeError("Tool-call limit reached")
        self.run.tool_calls += 1
        if not self.sources_ready():
            return json.dumps(
                {
                    "state": "blocked",
                    "errors": [
                        "Read every source successfully before requesting validation"
                    ],
                    "attempts_remaining": self.settings.max_validation_attempts
                    - len(self.run.validation_reports),
                }
            )
        if len(self.run.validation_reports) >= self.settings.max_validation_attempts:
            return json.dumps(
                {
                    "state": "attempt_limit_reached",
                    "attempts_remaining": 0,
                    "message": "Return the best current candidate. Unresolved findings will be noted for human review; they do not block the draft.",
                }
            )
        report = {
            "state": "incomplete",
            "evidence_review_complete": False,
            "deterministic_findings": [],
            "review_findings": [],
            "errors": [],
        }
        # Every submission consumes an attempt, including malformed candidates.
        self.run.validation_reports.append(report)
        try:
            args = json.loads(arguments)
            Draft202012Validator(self.parameters).validate(args)
            candidate = args["candidate"]
            self.run.response = deepcopy(candidate)
            report["candidate_sha256"] = candidate_hash(candidate)
            problems = candidate_errors(candidate, self.sources.list_files()["sources"])
            problems += [
                "Reported incomplete reading: " + issue["source_id"]
                for issue in candidate["extraction_issues"]
                if issue["issue_type"] in {"unreadable_content", "unsupported_format"}
            ]
            if problems:
                report["errors"] = problems
            else:
                self.store.structured = self.structured_evidence()
                enriched = self.store.assemble(
                    candidate,
                    self.sources.list_files()["sources"],
                    self.run.run_id,
                )
                reviewed = await self.reviewer.validate(
                    enriched, self.sources.root, self.client
                )
                report.update(reviewed)
                # Keep the raw-candidate digest as well as the enriched-extraction digest.
                report["candidate_sha256"] = candidate_hash(candidate)
        except ValidationError as error:
            report["errors"].append(
                "Invalid candidate at "
                + ".".join(map(str, error.absolute_path))
                + " ("
                + error.validator
                + ")"
            )
        except Exception as error:
            report["errors"].append(
                "Candidate validation failed: " + type(error).__name__
            )
        feedback = {
            key: report[key]
            for key in (
                "state",
                "evidence_review_complete",
                "deterministic_findings",
                "review_findings",
                "errors",
            )
        }
        return json.dumps(
            {
                **feedback,
                "attempts_remaining": self.settings.max_validation_attempts
                - len(self.run.validation_reports),
            },
            ensure_ascii=False,
        )
