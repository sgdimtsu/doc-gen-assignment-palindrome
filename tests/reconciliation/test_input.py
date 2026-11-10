"""Boundary checks with constructed evidence and simulated review verdicts."""

from copy import deepcopy
import hashlib
import json

import pytest

from agent_pipeline.extraction.models import ExtractionRun, ExtractionSettings
from agent_pipeline.extraction.results import ExtractionRecords, save_run, write_json
from agent_pipeline.reconciliation.input import load_extraction
from agent_pipeline.validation.status import candidate_hash


@pytest.fixture
def saved_run(tmp_path):
    inventory = [
        {
            "source_id": "source-1",
            "filename": "report_request.docx",
            "format": "docx",
            "read_status": "read",
        },
        {
            "source_id": "source-2",
            "filename": "template_spec.md",
            "format": "markdown",
            "read_status": "read",
        },
    ]
    candidate = {
        "sources": [
            {"source_id": s["source_id"], "role": "unknown", "document_date": None}
            for s in inventory
        ],
        "observations": [
            {
                "observation_type": "unclassified",
                "source_id": "source-1",
                "source_text": "An unclear reference.",
                "sections": [],
                "evidence_scope": "unclear",
                "data": {"reason_unclassified": "unclear_meaning"},
                "field_states": [],
            }
        ],
        "extraction_issues": [
            {
                "issue_type": "ambiguous_reference",
                "source_id": "source-1",
                "observation_indexes": [0],
                "affected_fields": [],
                "description": "Account identity is unclear.",
            }
        ],
    }
    run = ExtractionRun(state="complete", inventory=inventory, response=candidate)
    extraction = ExtractionRecords().assemble(run.response, run.inventory, run.run_id)
    run.validation_reports = [
        {
            "state": "passed",
            "evidence_review_complete": True,
            "errors": [],
            "deterministic_findings": [],
            "review_findings": [],
            "candidate_sha256": candidate_hash(candidate),
            "extraction_sha256": digest(extraction),
        }
    ]
    return save_run(tmp_path, "example", run, ExtractionSettings(model="offline-test"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read(folder, name):
    return json.loads((folder / name).read_text())


def test_loads_exact_approved_evidence_and_preserves_ambiguity(saved_run):
    before = {p.name: p.read_bytes() for p in saved_run.iterdir()}
    expected = read(saved_run, "extraction.json")
    assert load_extraction(saved_run)[0] == expected
    assert (
        load_extraction(saved_run)[0]["extraction_issues"][0]["issue_type"]
        == "ambiguous_reference"
    )
    assert {p.name: p.read_bytes() for p in saved_run.iterdir()} == before


@pytest.mark.parametrize(
    "failure",
    [
        "partial",
        "missing_file",
        "no_review",
        "incomplete_review",
        "error_finding",
        "changed_candidate",
    ],
)
def test_advisory_review_allows_available_input(saved_run, failure):
    if failure == "partial":
        summary = read(saved_run, "summary.json")
        summary["state"] = "partial"
        write_json(saved_run / "summary.json", summary)
    elif failure == "missing_file":
        (saved_run / "extraction.json").rename(saved_run / "partial_extraction.json")
    elif failure == "changed_candidate":
        extraction = read(saved_run, "extraction.json")
        extraction["observations"][0]["source_text"] = "Changed after approval."
        write_json(saved_run / "extraction.json", extraction)
    else:
        reports = read(saved_run, "validation.json")
        if failure == "no_review":
            reports = []
        elif failure == "incomplete_review":
            reports[-1]["evidence_review_complete"] = False
        else:
            reports[-1]["review_findings"] = [
                {
                    "category": "omission",
                    "severity": "error",
                    "message": "Relevant background needs checking.",
                    "observation_ids": [],
                }
            ]
        write_json(saved_run / "validation.json", reports)
    extraction, reports, errors = load_extraction(saved_run)
    from agent_pipeline.reconciliation.engine import reconcile

    result = reconcile(extraction, reports, errors)
    assert result["requires_review"]
    assert result["issues"]


@pytest.mark.parametrize(
    "failure",
    [
        "schema",
        "duplicate_id",
        "unknown_source",
        "unknown_observation",
    ],
)
def test_checks_structure_and_references_even_with_matching_digest(saved_run, failure):
    extraction = deepcopy(read(saved_run, "extraction.json"))
    if failure == "schema":
        del extraction["observations"][0]["data"]
    elif failure == "duplicate_id":
        extraction["observations"].append(deepcopy(extraction["observations"][0]))
    elif failure == "unknown_source":
        extraction["observations"][0]["source_id"] = "invented"
    elif failure == "unknown_observation":
        extraction["extraction_issues"][0]["observation_ids"] = ["invented"]
    write_json(saved_run / "extraction.json", extraction)
    reports = read(saved_run, "validation.json")
    reports[-1]["extraction_sha256"] = digest(extraction)
    write_json(saved_run / "validation.json", reports)
    with pytest.raises(ValueError):
        load_extraction(saved_run)
