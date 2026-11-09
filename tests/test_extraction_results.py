from copy import deepcopy
import json
from pathlib import Path

import pytest

from agent_pipeline.extraction_agent import ExtractionRun, ExtractionSettings
from agent_pipeline.extraction_results import ExtractionResultStore


@pytest.fixture
def complete_run():
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
    location = {
        key: None
        for key in ["json_path", "paragraph", "table", "row", "page", "region", "line"]
    }
    location["paragraph"] = 1
    return ExtractionRun(
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
                    "location": location,
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
                    "location": deepcopy(location),
                    "affected_fields": [],
                    "description": "The intended meaning is unclear.",
                    "observation_indexes": [0],
                }
            ],
        },
    )


@pytest.fixture
def store(tmp_path):
    return ExtractionResultStore(tmp_path / "outputs")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(store, run):
    return store.save("client_01", run, ExtractionSettings(model="offline-test"))


def test_assembly_owns_metadata_ids_and_bidirectional_links(store, complete_run):
    original = deepcopy(complete_run)
    result = store.assemble(complete_run, "run123")
    observation = result["observations"][0]
    issue = result["extraction_issues"][0]
    assert result["sources"][0]["filename"] == "report_request.docx"
    assert observation["observation_id"] == "run123:observation-0001"
    assert issue["issue_id"] == "run123:issue-0001"
    assert observation["issue_ids"] == [issue["issue_id"]]
    assert issue["observation_ids"] == [observation["observation_id"]]
    assert "observation_indexes" not in issue
    assert complete_run == original


def test_complete_save_publishes_result_and_run_manifest(store, complete_run):
    saved = save(store, complete_run)
    manifest = read(saved.current_manifest_path)
    assert saved.state == "complete"
    assert read(saved.result_path) == read(
        store.output_dir / "client_01/extraction.json"
    )
    assert read(saved.manifest_path) == manifest
    assert manifest["latest_complete_run"]["run_id"] == saved.run_id
    assert (
        manifest["result_path"]
        == saved.result_path.relative_to(store.output_dir / "client_01").as_posix()
    )
    assert manifest["settings"]["model"] == "offline-test"


def test_partial_run_cannot_overwrite_complete_result(store, complete_run):
    successful = save(store, complete_run)
    canonical = store.output_dir / "client_01/extraction.json"
    before = canonical.read_bytes()
    complete_run.state = "partial"
    complete_run.inventory[1]["read_status"] = "failed"
    complete_run.errors = ["Reading failed"]
    partial = save(store, complete_run)
    assert partial.state == "partial"
    assert "diagnostics" in partial.result_path.parts
    assert canonical.read_bytes() == before
    manifest = read(partial.current_manifest_path)
    assert manifest["state"] == "partial"
    assert manifest["latest_complete_run"]["run_id"] == successful.run_id
    assert manifest["run_id"] != successful.run_id
    assert successful.result_path.exists()


def test_invalid_response_saved_only_as_diagnostics(store, complete_run):
    complete_run.response = {"unexpected": True}
    complete_run.last_output_text = "invalid response"
    saved = save(store, complete_run)
    assert saved.state == "partial"
    assert saved.result_path is None
    assert not (store.output_dir / "client_01/extraction.json").exists()
    diagnostic = (
        store.output_dir
        / "client_01/diagnostics"
        / saved.run_id
        / "model_response.json"
    )
    assert read(diagnostic)["last_output_text"] == "invalid response"


@pytest.mark.parametrize("index", [-1, 1])
def test_out_of_range_issue_links_cannot_be_saved_as_complete(
    store, complete_run, index
):
    complete_run.response["extraction_issues"][0]["observation_indexes"] = [index]
    saved = save(store, complete_run)
    assert saved.state == "partial"
    assert saved.result_path is None


def test_unreadable_issue_prevents_complete_publication(store, complete_run):
    complete_run.response["extraction_issues"][0]["issue_type"] = "unreadable_content"
    saved = save(store, complete_run)
    assert saved.state == "partial"
    assert not (store.output_dir / "client_01/extraction.json").exists()


def test_partial_inventory_is_retained_without_invented_classification(
    store, complete_run
):
    complete_run.state = "partial"
    complete_run.response["sources"].pop()
    complete_run.inventory[1]["read_status"] = "pending"
    saved = save(store, complete_run)
    source = read(saved.result_path)["sources"][1]
    assert source["role"] == "unknown"
    assert source["document_date"] is None
    assert source["read_status"] == "pending"


def test_no_response_still_saves_failure_manifest(store):
    saved = save(store, ExtractionRun(errors=["Model-call limit reached"]))
    assert saved.state == "partial"
    assert saved.result_path is None
    assert "Model-call limit reached" in read(saved.manifest_path)["errors"]


def test_ids_do_not_collide_between_runs(store, complete_run):
    first = save(store, complete_run)
    second = save(store, complete_run)
    assert (
        read(first.result_path)["observations"][0]["observation_id"]
        != read(second.result_path)["observations"][0]["observation_id"]
    )


@pytest.mark.parametrize("client_name", ["../escape", "a/b", "..", "", "a\\b"])
def test_client_output_name_cannot_escape_folder(store, complete_run, client_name):
    with pytest.raises(ValueError):
        store.save(client_name, complete_run, ExtractionSettings(model="offline-test"))


def test_atomic_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "existing.json"
    path.write_text('{"previous": true}', encoding="utf-8")

    def fail_replace(*args):
        raise OSError("Simulated filesystem failure")

    monkeypatch.setattr("agent_pipeline.extraction_results.os.replace", fail_replace)
    with pytest.raises(OSError):
        ExtractionResultStore._write_json(path, {"new": True})
    assert read(path) == {"previous": True}
    assert list(tmp_path.iterdir()) == [path]
