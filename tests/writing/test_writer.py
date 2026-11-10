from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from agent_pipeline import ROOT, generate
from agent_pipeline.writing.writer import FCA_LINE, write_report


SPEC = """# Report specification: Example advice

## Your account
Keep the FCA authorisation sentence as written.

## Disposal details
Include ONLY if a disposal is recommended.

## Closing
Must include this warning, word for word:
"This is the required warning.
Preserve both sentences."
"""


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(self.result), refusal=None
                    ),
                )
            ]
        )


def result():
    return {
        "sections": [
            {
                "title": "Your account",
                "include": True,
                "content": "The supplied account facts.",
                "review_notes": [
                    {
                        "issue_index": 0,
                        "text": "Confirm the ongoing charge before proceeding.",
                    }
                ],
            },
            {
                "title": "Disposal details",
                "include": False,
                "content": "",
                "review_notes": [],
            },
            {
                "title": "Closing",
                "include": True,
                "content": "Please review this draft.",
                "review_notes": [],
            },
        ]
    }


def config():
    return json.loads((ROOT / "config/template_config_v2.json").read_text())


def facts():
    return {
        "validation_state": "failed",
        "requires_review": True,
        "issues": [
            {
                "issue_type": "confirmation_required",
                "severity": "error",
                "message": "Charge unknown.",
            }
        ],
    }


def test_client_spec_controls_structure_and_code_preserves_wording_and_notes():
    client = FakeClient(result())
    request = {
        "blocks": [{"cells": ["Agreed risk profile", "4 (moderate)"]}],
        "limitations": [],
    }
    report = write_report(client, "test-model", SPEC, request, facts(), config())
    assert "## Your account\n" in report and "## Closing\n" in report
    assert "## Disposal details" not in report
    assert "## Introduction" not in report
    assert report.index("## Your account") < report.index("## Closing")
    assert FCA_LINE in report
    assert "This is the required warning. Preserve both sentences." in report
    assert "[REVIEW REQUIRED: Confirm the ongoing charge before proceeding.]" in report
    assert "DRAFT — FOR HUMAN REVIEW" in report
    payload = json.loads(client.requests[0]["messages"][1]["content"])
    assert payload["report_request"] == request
    assert payload["reconciled_facts"] == facts()
    assert payload["template_spec"] == SPEC
    assert client.requests[0]["store"] is False
    assert len(client.requests) == 1
    assert "sections" not in config()


def test_another_client_can_change_headings_without_changing_config():
    spec = SPEC.replace("Your account", "Different scope").replace(
        "Closing", "Next steps"
    )
    data = result()
    data["sections"][0]["title"] = "Different scope"
    data["sections"][2]["title"] = "Next steps"
    report = write_report(FakeClient(data), "test", spec, {}, facts(), config())
    assert "## Different scope" in report and "## Next steps" in report


@pytest.mark.parametrize(
    "failure",
    [
        "section_order",
        "extra_heading",
    ],
)
def test_malformed_writer_structure_is_rejected(failure):
    data = result()
    if failure == "section_order":
        data["sections"].reverse()
    else:
        data["sections"][0][
            "content"
        ] = "## An extra report section\nUnexpected structure."
    with pytest.raises(ValueError):
        write_report(FakeClient(data), "test", SPEC, {}, facts(), config())


def test_fixed_wording_is_not_duplicated():
    data = result()
    data["sections"][0]["content"] += "\n\n" + FCA_LINE
    report = write_report(FakeClient(data), "test", SPEC, {}, facts(), config())
    assert report.count(FCA_LINE) == 1


def test_no_issues_requires_no_artificial_review_notes():
    data = result()
    data["sections"][0]["review_notes"] = []
    report = write_report(
        FakeClient(data),
        "test",
        SPEC,
        {},
        {"issues": [], "validation_state": "passed"},
        config(),
    )
    assert "REVIEW REQUIRED" not in report


@pytest.fixture
def writer_run(tmp_path, monkeypatch):
    client_dir = tmp_path / "data" / "example"
    client_dir.mkdir(parents=True)
    (client_dir / "template_spec.md").write_text(SPEC)
    with ZipFile(client_dir / "report_request.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Agreed risk profile</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>4 (moderate)</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>",
        )
    folder = tmp_path / "outputs" / "example" / "run1"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(
        json.dumps({"client": "example", "state": "partial"})
    )
    (folder / "reconciliation.json").write_text(json.dumps(facts()))
    client = FakeClient(result())
    monkeypatch.setattr(generate, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(generate, "load_environment", lambda: None)
    return (
        folder,
        client,
        [
            "--client",
            "example",
            "--data-dir",
            str(tmp_path / "data"),
            "--run-dir",
            str(folder),
        ],
    )


def test_command_writes_run_report_from_request_spec_and_reconciliation(writer_run):
    folder, client, args = writer_run
    generate.main(args + ["--model", "chosen-model"])
    assert (folder / "report.md").exists()
    assert client.requests[0]["model"] == "chosen-model"
    payload = json.loads(client.requests[0]["messages"][1]["content"])
    assert payload["report_request"]["blocks"] == [
        {"cells": ["Agreed risk profile", "4 (moderate)"]}
    ]
    assert payload["reconciled_facts"]["validation_state"] == "failed"


def test_command_rejects_mixed_client_inputs_before_any_api_call(writer_run):
    folder, client, args = writer_run
    (folder / "summary.json").write_text(json.dumps({"client": "another-client"}))
    with pytest.raises(SystemExit):
        generate.main(args)
    assert client.requests == []


def test_one_command_extracts_reconciles_and_writes_despite_advisory_errors(
    writer_run, monkeypatch
):
    from agent_pipeline.extraction.models import ExtractionRun
    from agent_pipeline.extraction.results import ExtractionRecords
    from agent_pipeline.extraction.sources import SourceTools
    from agent_pipeline.validation.status import candidate_hash
    import hashlib

    folder, _, args = writer_run
    pipeline_args = args[:-2] + [
        "--output-dir",
        str(folder.parent.parent),
        "--model",
        "pipeline-model",
    ]
    async_client = SimpleNamespace(closed=False)

    async def close():
        async_client.closed = True

    async_client.close = close
    monkeypatch.setattr(generate, "AsyncOpenAI", lambda: async_client)

    class Extraction:
        def __init__(self, client, settings):
            assert client is async_client
            assert settings.model == "pipeline-model"

        async def run(self, client_dir):
            inventory = SourceTools(client_dir).list_files()["sources"]
            for source in inventory:
                source["read_status"] = "read"
            candidate = {
                "sources": [
                    {
                        "source_id": s["source_id"],
                        "role": "unknown",
                        "document_date": None,
                    }
                    for s in inventory
                ],
                "observations": [],
                "extraction_issues": [],
            }
            run = ExtractionRun(
                state="complete", response=candidate, inventory=inventory
            )
            assembled = ExtractionRecords().assemble(
                run.response, run.inventory, run.run_id
            )
            run.validation_reports = [
                {
                    "state": "failed",
                    "evidence_review_complete": True,
                    "errors": [],
                    "candidate_sha256": candidate_hash(candidate),
                    "extraction_sha256": hashlib.sha256(
                        json.dumps(assembled, sort_keys=True).encode()
                    ).hexdigest(),
                    "deterministic_findings": [],
                    "review_findings": [
                        {
                            "category": "omission",
                            "severity": "error",
                            "message": "Check a missing source fact.",
                            "observation_ids": [],
                        }
                    ],
                }
            ]
            return run

    monkeypatch.setattr(generate, "ExtractionAgent", Extraction)

    class Writer(FakeClient):
        def create(self, **kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            self.result["sections"][0]["review_notes"] = [
                {
                    "issue_index": i,
                    "text": "Please verify this source concern before proceeding.",
                }
                for i, _ in enumerate(payload["reconciled_facts"]["issues"])
            ]
            return super().create(**kwargs)

    writer = Writer(result())
    monkeypatch.setattr(generate, "OpenAI", lambda **kwargs: writer)
    assert generate.main(pipeline_args) == 0
    generated = [p for p in folder.parent.iterdir() if p != folder]
    assert len(generated) == 1
    run_dir = generated[0]
    assert {
        "summary.json",
        "validation.json",
        "extraction.json",
        "reconciliation.json",
        "report.md",
    } <= {p.name for p in run_dir.iterdir()}
    assert (
        json.loads((run_dir / "summary.json").read_text())["validation_state"]
        == "failed"
    )
    assert "REVIEW REQUIRED" in (run_dir / "report.md").read_text()
    assert async_client.closed
    assert writer.requests[0]["model"] == "pipeline-model"
    assert len(writer.requests) == 1


def test_risk_warning_precedes_closing_text():
    report = write_report(FakeClient(result()), "test", SPEC, {}, facts(), config())
    assert report.index("This is the required warning.") < report.index(
        "Please review this draft."
    )
