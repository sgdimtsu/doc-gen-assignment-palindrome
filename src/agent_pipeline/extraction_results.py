"""Code-owned enrichment and persistence of extraction runs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from agent_pipeline.extraction_agent import ExtractionRun, ExtractionSettings, ROOT
from agent_pipeline.source_tools import REQUIRED_FILES


@dataclass(frozen=True)
class SavedExtraction:
    state: str
    run_id: str
    result_path: Path | None
    manifest_path: Path
    current_manifest_path: Path


class ExtractionResultStore:
    def __init__(
        self,
        output_dir: Path,
        schema_path: Path = ROOT / "config/extraction_schema.json",
    ):
        self.output_dir = output_dir.resolve()
        self.response_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.response_validator = Draft202012Validator(self.response_schema)
        self.result_schema = self._result_schema(self.response_schema)
        Draft202012Validator.check_schema(self.result_schema)
        self.result_validator = Draft202012Validator(self.result_schema)

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
                        "issue_ids": {"type": "array", "items": {"type": "string"}},
                    }
                )
                definition["required"] += ["observation_id", "issue_ids"]
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
        return schema

    def assemble(self, run: ExtractionRun, run_id: str) -> dict:
        self.response_validator.validate(run.response)
        response = deepcopy(run.response)
        inventory = {source["source_id"]: source for source in run.inventory}
        if len(inventory) != len(run.inventory):
            raise ValueError("Duplicate source IDs in code inventory")
        classifications = {
            source["source_id"]: source for source in response["sources"]
        }
        if len(classifications) != len(response["sources"]):
            raise ValueError("Duplicate source classifications")
        if set(classifications) - set(inventory):
            raise ValueError("Response references sources outside the inventory")
        if run.state == "complete" and set(classifications) != set(inventory):
            raise ValueError("Complete response does not cover the source inventory")
        sources = []
        for sid, metadata in inventory.items():
            classification = classifications.get(
                sid,
                {
                    "source_id": sid,
                    "role": "unknown",
                    "document_date": None,
                },
            )
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
            if observation["source_id"] not in inventory:
                raise ValueError("Observation references an unknown source")
            observation["observation_id"] = f"{run_id}:observation-{index + 1:04d}"
            observation["issue_ids"] = []
        issues = response["extraction_issues"]
        for index, issue in enumerate(issues):
            if issue["source_id"] not in inventory:
                raise ValueError("Issue references an unknown source")
            issue_id = f"{run_id}:issue-{index + 1:04d}"
            indexes = issue.pop("observation_indexes")
            if len(indexes) != len(set(indexes)):
                raise ValueError("Issue contains duplicate observation links")
            if any(i < 0 or i >= len(observations) for i in indexes):
                raise ValueError("Issue observation index is out of range")
            issue["issue_id"] = issue_id
            issue["observation_ids"] = [
                observations[i]["observation_id"] for i in indexes
            ]
            for i in indexes:
                observations[i]["issue_ids"].append(issue_id)
        result = {
            "sources": sources,
            "observations": observations,
            "extraction_issues": issues,
        }
        self.result_validator.validate(result)
        return result

    @staticmethod
    def _write_json(path: Path, value):
        # Serialise before touching the destination; replace only a complete file.
        text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def save(
        self, client_name: str, run: ExtractionRun, settings: ExtractionSettings
    ) -> SavedExtraction:
        if (
            not client_name
            or client_name in {".", ".."}
            or Path(client_name).name != client_name
            or "\\" in client_name
        ):
            raise ValueError("Client name must be a single folder name")
        folder = (self.output_dir / client_name).resolve()
        if not folder.is_relative_to(self.output_dir):
            raise ValueError("Client output resolves outside the output directory")
        run_id = uuid4().hex
        errors = list(run.errors)
        result = None
        if run.state not in {"complete", "partial"}:
            errors.append("Unknown extraction state")
        if run.response is not None:
            try:
                result = self.assemble(run, run_id)
            except ValidationError:
                errors.append("Saved-result assembly failed schema validation")
            except (ValueError, KeyError, TypeError) as error:
                errors.append("Saved-result assembly failed: " + type(error).__name__)
        else:
            errors.append("No structured extraction response available")
        state = (
            "complete"
            if run.state == "complete" and not errors and result is not None
            else "partial"
        )
        if state == "complete":
            covered = {
                s["filename"] for s in run.inventory if s["read_status"] == "read"
            }
            if not set(REQUIRED_FILES).issubset(covered) or any(
                s["read_status"] != "read" for s in run.inventory
            ):
                state = "partial"
                errors.append("Complete extraction requires full source coverage")
            if any(
                issue["issue_type"] in {"unreadable_content", "unsupported_format"}
                for issue in result["extraction_issues"]
            ):
                state = "partial"
                errors.append("Extraction contains unresolved reading limitations")
        run_folder = folder / "runs" / run_id
        result_path = None
        if result is not None:
            result_path = (
                run_folder / "extraction.json"
                if state == "complete"
                else folder / "diagnostics" / run_id / "partial_extraction.json"
            )
            self._write_json(result_path, result)
        if state == "partial" and (
            run.response is not None or run.last_output_text is not None
        ):
            self._write_json(
                folder / "diagnostics" / run_id / "model_response.json",
                {
                    "structured_response": run.response,
                    "last_output_text": run.last_output_text,
                },
            )
        current_manifest = folder / "extraction_manifest.json"
        previous_complete = None
        if current_manifest.exists():
            previous = json.loads(current_manifest.read_text(encoding="utf-8"))
            previous_complete = previous.get("latest_complete_run")
        relative_result = (
            result_path.relative_to(folder).as_posix() if result_path else None
        )
        if state == "complete":
            previous_complete = {"run_id": run_id, "result_path": relative_result}
        manifest = {
            "run_id": run_id,
            "client": client_name,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "state": state,
            "result_path": relative_result,
            "latest_complete_run": previous_complete,
            "inventory": deepcopy(run.inventory),
            "errors": errors,
            "tool_errors": deepcopy(run.tool_errors),
            "tool_events": deepcopy(run.tool_events),
            "settings": asdict(settings),
            "usage": deepcopy(run.usage),
            "model_calls": run.model_calls,
            "tool_calls": run.tool_calls,
            "elapsed_seconds": run.elapsed_seconds,
        }
        manifest_path = run_folder / "manifest.json"
        self._write_json(manifest_path, manifest)
        if state == "complete":
            self._write_json(folder / "extraction.json", result)
        # Publish the manifest last; it identifies the immutable result for this run.
        self._write_json(current_manifest, manifest)
        return SavedExtraction(
            state, run_id, result_path, manifest_path, current_manifest
        )
