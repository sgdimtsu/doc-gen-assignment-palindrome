"""Load one explicit run without treating validation findings as a release gate."""

import json
from pathlib import Path

from jsonschema import ValidationError
from agent_pipeline.extraction.results import ExtractionRecords


def load_extraction(run_dir: Path):
    try:
        path = run_dir / "extraction.json"
        if not path.exists():
            path = run_dir / "partial_extraction.json"
        extraction = json.loads(path.read_text(encoding="utf-8"))
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        validation_path = run_dir / "validation.json"
        reports = (
            json.loads(validation_path.read_text(encoding="utf-8"))
            if validation_path.exists()
            else []
        )
        if (
            not isinstance(summary, dict)
            or not isinstance(reports, list)
            or any(not isinstance(r, dict) for r in reports)
        ):
            raise ValueError("Invalid run metadata")
        ExtractionRecords().result_validator.validate(extraction)
    except (OSError, ValueError, ValidationError) as error:
        raise ValueError(
            "Run requires structurally valid extraction and readable run metadata"
        ) from error
    for collection, key in (
        ("sources", "source_id"),
        ("observations", "observation_id"),
        ("extraction_issues", "issue_id"),
    ):
        ids = [record[key] for record in extraction[collection]]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate IDs in " + collection)
    source_ids = {s["source_id"] for s in extraction["sources"]}
    observation_ids = {o["observation_id"] for o in extraction["observations"]}
    for record in extraction["observations"] + extraction["extraction_issues"]:
        if record["source_id"] not in source_ids:
            raise ValueError("Extraction references an unknown source")
    for issue in extraction["extraction_issues"]:
        if not set(issue["observation_ids"]).issubset(observation_ids):
            raise ValueError("Issue references an unknown observation")
    errors = list(summary.get("errors", []))
    if summary.get("state") != "complete":
        errors.append(
            "Extraction execution was incomplete; available evidence requires review"
        )
    return extraction, reports, errors
