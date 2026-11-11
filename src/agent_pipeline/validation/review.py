"""Load source evidence, run checks and request a bounded model review."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from agent_pipeline import ROOT
from agent_pipeline.validation.checks import check_extraction, normalise_text
from agent_pipeline.extraction.results import ExtractionRecords
from agent_pipeline.extraction.sources import SourceTools, REQUIRED_FILES


@dataclass(frozen=True)
class ValidationSettings:
    model: str = "gpt-4o-mini"
    max_output_tokens: int = 8000
    timeout_seconds: float = 90
    api_retries: int = 2
    max_evidence_bytes: int = 4_000_000

    def __post_init__(self):
        if (
            not self.model.strip()
            or min(
                self.max_output_tokens, self.timeout_seconds, self.max_evidence_bytes
            )
            <= 0
            or self.api_retries < 0
        ):
            raise ValueError("Invalid validation settings")


class ExtractionValidator:
    def __init__(
        self,
        settings=ValidationSettings(),
        extraction_schema_path=ROOT / "config/extraction_schema.json",
        review_schema_path=ROOT / "config/extraction_validation_schema.json",
        instructions_path=ROOT / "EXTRACTION_VALIDATION.md",
    ):
        self.settings = settings
        response_schema = json.loads(Path(extraction_schema_path).read_text())
        self.field_definitions = {
            name: response_schema["$defs"][name]
            for name in ("account_reference", "field_state")
        }
        self.extraction_validator = Draft202012Validator(
            ExtractionRecords._result_schema(response_schema)
        )
        self.review_schema = json.loads(Path(review_schema_path).read_text())
        Draft202012Validator.check_schema(self.review_schema)
        self.review_validator = Draft202012Validator(self.review_schema)
        self.instructions = Path(instructions_path).read_text()

    def _load_evidence(self, client_dir, extraction):
        tools = SourceTools(client_dir)
        inventory = tools.list_files()
        if inventory["missing_required_files"]:
            raise ValueError("Required relevance files are missing")
        current = {s["filename"]: s for s in inventory["sources"]}
        original = {s["filename"]: s for s in extraction["sources"]}
        if len(original) != len(extraction["sources"]) or set(original) != set(current):
            raise ValueError(
                "Current source inventory differs from extraction inventory"
            )
        evidence = {}
        ordered = list(REQUIRED_FILES) + [
            name for name in current if name not in REQUIRED_FILES
        ]
        for name in ordered:
            result = tools.read_source(current[name]["source_id"])
            if not result.get("ok") or not result.get("complete"):
                raise ValueError(
                    "Cannot review all evidence: source reading is incomplete"
                )
            sid = original[name]["source_id"]
            text = result.get("raw_text") or result.get("text")
            if text is None and "blocks" in result:
                text = "\n".join(
                    block.get("text", "\n".join(block.get("cells", [])))
                    for block in result["blocks"]
                )
            image = result.get("image")
            evidence[sid] = {
                "source_id": sid,
                "filename": name,
                "format": current[name]["format"],
                "text": text,
                "image": image,
                "code_records": tools.protected.get(current[name]["source_id"], []),
            }
        return evidence

    def _response_schema(self, evidence, extraction):
        schema = deepcopy(self.review_schema)
        source_ids = list(evidence)
        reviewed = schema["properties"]["reviewed_source_ids"]
        reviewed["items"]["enum"] = source_ids
        reviewed["minItems"] = reviewed["maxItems"] = len(source_ids)
        finding = schema["properties"]["findings"]["items"]["properties"]
        finding["source_ids"]["items"]["enum"] = source_ids
        finding["evidence"]["items"]["properties"]["source_id"]["enum"] = source_ids
        observation_ids = [o["observation_id"] for o in extraction["observations"]]
        if observation_ids:
            finding["observation_ids"]["items"]["enum"] = observation_ids
        else:
            finding["observation_ids"]["maxItems"] = 0
        return schema

    @staticmethod
    def _review_errors(review, evidence, extraction):
        errors = []
        returned = review["reviewed_source_ids"]
        missing = sorted(set(evidence) - set(returned))
        unexpected = sorted(set(returned) - set(evidence))
        duplicates = sorted(
            sid for sid, count in Counter(returned).items() if count > 1
        )
        if missing or unexpected or duplicates:
            errors.append(
                f"Source coverage mismatch: missing={missing}, "
                f"unexpected={unexpected}, duplicates={duplicates}"
            )
        known_observations = {o["observation_id"] for o in extraction["observations"]}
        for index, finding in enumerate(review["findings"]):
            label = f"findings[{index}]"
            if (
                not finding["source_ids"]
                or not set(finding["source_ids"]).issubset(evidence)
                or not set(finding["observation_ids"]).issubset(known_observations)
            ):
                errors.append(label + ": invalid source or observation references")
            if not finding["evidence"] and finding["category"] != "missing_information":
                errors.append(label + ": source evidence is required for this finding")
            for excerpt_index, excerpt in enumerate(finding["evidence"]):
                source = evidence.get(excerpt["source_id"])
                excerpt_label = f"{label}.evidence[{excerpt_index}]"
                if (
                    not source
                    or excerpt["source_id"] not in finding["source_ids"]
                    or not excerpt["source_text"].strip()
                ):
                    errors.append(
                        excerpt_label + ": invalid evidence reference or empty excerpt"
                    )
                elif source["text"] is not None and normalise_text(
                    excerpt["source_text"]
                ) not in normalise_text(source["text"]):
                    errors.append(
                        excerpt_label
                        + ": excerpt is not verbatim in "
                        + excerpt["source_id"]
                    )
        return errors

    async def validate(self, extraction, client_dir, client=None):
        report = {
            "state": "incomplete",
            "deterministic_findings": [],
            "review_findings": [],
            "evidence_review_complete": False,
            "errors": [],
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "model": self.settings.model,
            "model_calls": 0,
            "extraction_sha256": hashlib.sha256(
                json.dumps(extraction, sort_keys=True).encode()
            ).hexdigest(),
        }
        try:
            self.extraction_validator.validate(extraction)
            evidence = self._load_evidence(client_dir, extraction)
        except (ValueError, OSError, ValidationError) as error:
            report["errors"].append(
                "Cannot load validation inputs: "
                + (str(error) if type(error) is ValueError else type(error).__name__)
            )
            return report
        report["deterministic_findings"] = check_extraction(
            extraction, evidence, self.extraction_validator
        )
        if client is None:
            report["state"] = (
                "failed" if report["deterministic_findings"] else "not_reviewed"
            )
            return report
        report["review_diagnostics"] = {
            "response": None,
            "rejection_reasons": [],
        }
        content = [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "extraction_field_definitions": self.field_definitions,
                        "expected_reviewed_source_ids": list(evidence),
                        "extraction": extraction,
                        "sources": [
                            {k: value for k, value in source.items() if k != "image"}
                            for source in evidence.values()
                        ],
                        "deterministic_findings": report["deterministic_findings"],
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        for sid, source in evidence.items():
            if source["image"]:
                content.extend(
                    [
                        {"type": "text", "text": "Source image: " + sid},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": source["image"]["image_url"],
                                "detail": "high",
                            },
                        },
                    ]
                )
        if len(json.dumps(content).encode()) > self.settings.max_evidence_bytes:
            report["errors"].append(
                "Evidence exceeds the configured review size; nothing was truncated"
            )
            return report
        try:
            configured = client.with_options(
                max_retries=self.settings.api_retries,
                timeout=self.settings.timeout_seconds,
            )
            report["model_calls"] = 1
            response = await configured.chat.completions.create(
                model=self.settings.model,
                store=False,
                messages=[
                    {"role": "system", "content": self.instructions},
                    {"role": "user", "content": content},
                ],
                max_completion_tokens=self.settings.max_output_tokens,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "extraction_review",
                        "strict": True,
                        "schema": self._response_schema(evidence, extraction),
                    },
                },
            )
            if response.usage:
                report["usage"] = {
                    "input_tokens": response.usage.prompt_tokens or 0,
                    "output_tokens": response.usage.completion_tokens or 0,
                }
            if (
                not response.choices
                or response.choices[0].finish_reason != "stop"
                or getattr(response.choices[0].message, "refusal", None)
            ):
                raise ValueError("Evidence review did not complete")
            output_text = response.choices[0].message.content or ""
            try:
                review = json.loads(output_text)
            except ValueError:
                report["review_diagnostics"]["raw_output_text"] = output_text
                raise ValueError("Reviewer returned invalid JSON") from None
            report["review_diagnostics"]["response"] = review
            # Validate against the base schema first so ID mismatches get precise diagnostics.
            self.review_validator.validate(review)
            problems = self._review_errors(review, evidence, extraction)
            report["review_diagnostics"]["rejection_reasons"] = problems
            if problems:
                raise ValueError("; ".join(problems))
            report["review_findings"] = review["findings"]
            report["evidence_review_complete"] = True
            all_findings = report["deterministic_findings"] + report["review_findings"]
            report["state"] = (
                "failed"
                if any(f["severity"] == "error" for f in all_findings)
                else "passed"
            )
        except Exception as error:
            if isinstance(error, ValidationError):
                report["review_diagnostics"]["rejection_reasons"] = [
                    "Review schema violation at "
                    + ".".join(map(str, error.absolute_path))
                    + " ("
                    + error.validator
                    + ")"
                ]
            report["errors"].append(
                "Evidence review failed: "
                + (str(error) if type(error) is ValueError else type(error).__name__)
            )
            if hasattr(error, "status_code"):
                report["errors"].append("API HTTP status: " + str(error.status_code))
        return report
