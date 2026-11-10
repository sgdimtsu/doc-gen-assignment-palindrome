"""Bounded candidate review and the final release gate."""

from copy import deepcopy
import hashlib
import json

from agents import FunctionTool
from jsonschema import Draft202012Validator, ValidationError

from agent_pipeline.extraction.models import ExtractionRun
from agent_pipeline.extraction.sources import SourceTools
from agent_pipeline.extraction.results import ExtractionRecords
from agent_pipeline.validation.review import ExtractionValidator, ValidationSettings
from agent_pipeline.validation.status import (
    candidate_hash,
    candidate_errors,
    release_errors,
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
            self.run.errors.append("Validation-attempt limit reached")
            raise RuntimeError("Validation-attempt limit reached")
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
            if set(self.sources.sources) - self.delivered:
                problems.append(
                    "Read every source successfully before requesting validation"
                )
            if problems:
                report["errors"] = problems
            else:
                enriched = self.store.assemble(
                    ExtractionRun(
                        response=candidate,
                        inventory=self.sources.list_files()["sources"],
                    ),
                    self.run.run_id,
                )
                reviewed = await self.reviewer.validate_async(
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
            key: value for key, value in report.items() if key != "review_diagnostics"
        }
        return json.dumps(
            {
                **feedback,
                "attempts_remaining": self.settings.max_validation_attempts
                - len(self.run.validation_reports),
            },
            ensure_ascii=False,
        )

    def final_errors(self, candidate):
        errors = release_errors(
            candidate, self.run.validation_reports, self.sources.list_files()["sources"]
        )
        if errors:
            return errors
        report = self.run.validation_reports[-1]
        current = self.sources.list_files()["sources"]
        # Check both inventory and file bytes: a pass cannot outlive changed evidence.
        fresh = SourceTools(self.sources.root)
        if [(s["source_id"], s["filename"]) for s in fresh.list_files()["sources"]] != [
            (s["source_id"], s["filename"]) for s in current
        ]:
            return ["Source inventory changed after validation"]
        if any(
            not path.resolve(strict=True).is_relative_to(fresh.root)
            for path in fresh._paths.values()
        ):
            return ["Source resolves outside the client folder after validation"]
        hashes = {
            sid: hashlib.sha256(path.read_bytes()).hexdigest()
            for sid, path in fresh._paths.items()
        }
        if hashes != report.get("source_hashes"):
            return ["Source evidence changed after validation"]
        return []
