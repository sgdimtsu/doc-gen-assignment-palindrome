from copy import deepcopy
import hashlib
import json
from uuid import uuid4

import pytest

from agent_pipeline.extraction.models import ExtractionRun, ExtractionSettings
from agent_pipeline.extraction.results import ExtractionRecords, save_run
from agent_pipeline.validation.status import candidate_hash


@pytest.fixture
def complete_run(records):
    inventory = [
        {
            "source_id": "source-0001",
            "filename": "report_request.docx",
            "format": "docx",
            "read_status": "read",
        },
        {
            "source_id": "source-0002",
            "filename": "template_spec.md",
            "format": "markdown",
            "read_status": "read",
        },
    ]
    run = ExtractionRun(
        state="complete",
        inventory=inventory,
        response={
            "sources": [
                {"source_id": s["source_id"], "role": "unknown", "document_date": None}
                for s in inventory
            ],
            "observations": [
                {
                    "observation_type": "unclassified",
                    "source_id": "source-0001",
                    "source_text": "A potentially relevant passage.",
                    "sections": [],
                    "evidence_scope": "client_specific",
                    "data": {"reason_unclassified": "uncertain_relevance"},
                    "field_states": [],
                }
            ],
            "extraction_issues": [
                {
                    "issue_type": "ambiguous_wording",
                    "source_id": "source-0001",
                    "affected_fields": [],
                    "description": "The intended meaning is unclear.",
                    "observation_indexes": [0],
                }
            ],
        },
    )

    mark_validated(records, run)
    return run


def mark_validated(records, run):
    run.validation_reports = [
        {
            "state": "passed",
            "evidence_review_complete": True,
            "errors": [],
            "deterministic_findings": [],
            "review_findings": [],
            "candidate_sha256": candidate_hash(run.response),
            "extraction_sha256": hashlib.sha256(
                json.dumps(
                    records.assemble(run.response, run.inventory, run.run_id),
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        }
    ]


@pytest.fixture
def records():
    return ExtractionRecords()


def read(path):
    return json.loads(path.read_text())


def save(tmp_path, run):
    return save_run(
        tmp_path / "outputs", "example", run, ExtractionSettings(model="offline-test")
    )


def test_assembly_adds_code_metadata_and_one_way_issue_links(records, complete_run):
    before = deepcopy(complete_run)
    result = records.assemble(
        complete_run.response, complete_run.inventory, complete_run.run_id
    )
    observation = result["observations"][0]
    issue = result["extraction_issues"][0]
    assert result["sources"][0]["filename"] == "report_request.docx"
    assert issue["observation_ids"] == [observation["observation_id"]]
    assert "observation_indexes" not in issue
    assert "issue_ids" not in observation
    assert complete_run == before


def test_complete_run_saves_only_one_set_of_stage_files(tmp_path, complete_run):
    folder = save(tmp_path, complete_run)
    assert {p.name for p in folder.iterdir()} == {
        "summary.json",
        "validation.json",
        "extraction.json",
    }
    assert read(folder / "summary.json")["state"] == "complete"
    assert read(folder / "validation.json") == complete_run.validation_reports
    assert folder == tmp_path / "outputs" / "example" / complete_run.run_id
    assert list(folder.parent.iterdir()) == [folder]


def test_partial_run_is_separate_and_cannot_replace_complete_run(
    tmp_path, complete_run
):
    first = save(tmp_path, complete_run)
    before = (first / "extraction.json").read_bytes()
    partial = deepcopy(complete_run)
    partial.run_id = uuid4().hex
    partial.errors = ["Stopped early"]
    second = save(tmp_path, partial)
    assert read(second / "summary.json")["state"] == "partial"
    assert (second / "partial_extraction.json").exists()
    assert not (second / "extraction.json").exists()
    assert (first / "extraction.json").read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "no_validation",
        "candidate",
        "metadata",
        "coverage",
        "reading_issue",
        "error_finding",
    ],
)
def test_validation_quality_is_separate_from_execution(tmp_path, complete_run, change):
    if change == "no_validation":
        complete_run.validation_reports = []
    elif change == "candidate":
        complete_run.response["sources"][0]["document_date"] = "2026-01-01"
    elif change == "metadata":
        complete_run.inventory[0]["format"] = "changed"
    elif change == "coverage":
        complete_run.inventory[0]["read_status"] = "partial"
    elif change == "reading_issue":
        complete_run.response["extraction_issues"][0][
            "issue_type"
        ] = "unreadable_content"
    else:
        complete_run.validation_reports[-1]["review_findings"] = [{"severity": "error"}]
    folder = save(tmp_path, complete_run)
    summary = read(folder / "summary.json")
    assert summary["state"] == ("partial" if change == "coverage" else "complete")
    assert (
        summary["validation_state"]
        == {
            "no_validation": "not_reviewed",
            "candidate": "stale",
            "metadata": "stale",
            "coverage": "stale",
            "reading_issue": "stale",
            "error_finding": "failed",
        }[change]
    )


def test_invalid_response_retained_for_diagnostics(tmp_path, complete_run):
    complete_run.response = {"unexpected": True}
    folder = save(tmp_path, complete_run)
    assert read(folder / "model_response.json")["response"] == {"unexpected": True}
    assert read(folder / "summary.json")["state"] == "partial"


@pytest.mark.parametrize("index", [-1, 1])
def test_invalid_issue_index_cannot_be_assembled(records, complete_run, index):
    complete_run.response["extraction_issues"][0]["observation_indexes"] = [index]
    with pytest.raises(ValueError):
        records.assemble(
            complete_run.response, complete_run.inventory, complete_run.run_id
        )


def test_failure_without_candidate_still_saves_summary(tmp_path):
    folder = save(tmp_path, ExtractionRun(errors=["Model-call limit reached"]))
    assert read(folder / "summary.json")["state"] == "partial"
    assert read(folder / "validation.json") == []
    assert not (folder / "extraction.json").exists()


@pytest.mark.parametrize("client_name", ["../escape", "a/b", "..", "", "a\\b"])
def test_output_cannot_escape_directory(tmp_path, complete_run, client_name):
    with pytest.raises(ValueError):
        save_run(
            tmp_path,
            client_name,
            complete_run,
            ExtractionSettings(model="offline-test"),
        )
