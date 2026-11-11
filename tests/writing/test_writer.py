"""The writer cannot change, add, omit or move reconciled facts."""

from copy import deepcopy
import json
import shutil
from types import SimpleNamespace

import pytest

from agent_pipeline import ROOT, generate
from agent_pipeline.extraction.models import ExtractionRun, ExtractionSettings
from agent_pipeline.extraction.results import save_run
from agent_pipeline.extraction.sources import SourceTools
from agent_pipeline.reconciliation.engine import reconcile
from agent_pipeline.reconciliation.report import FCA_LINE
from agent_pipeline.writing.writer import write_report, render_report, WriterOutputError


class FakeClient:
    def __init__(self, change=None):
        self.requests = []
        self.chat = SimpleNamespace(completions=self)
        self.change = change

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def create(self, **kwargs):
        self.requests.append(kwargs)
        plan = json.loads(kwargs["messages"][1]["content"])["reconciled_report"]
        result = {
            "sections": [
                {"title": s["title"], "block_ids": s["block_ids"], "paragraphs": []}
                for s in plan["sections"]
                if s["include"]
            ]
        }
        if self.change:
            self.change(result)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(result), refusal=None),
                )
            ]
        )


def config():
    return json.loads((ROOT / "config/template_config_v2.json").read_text())


@pytest.fixture
def inputs(tmp_path):
    folder = tmp_path / "data" / "example"
    shutil.copytree(ROOT / "data/client_01_clean", folder)
    sources = SourceTools(folder)
    inv = sources.list_files()["sources"]
    for name in ["report_request.docx", "template_spec.md"]:
        sources.read_source(next(s["source_id"] for s in inv if s["filename"] == name))
    for source in inv:
        if source["filename"] not in {"report_request.docx", "template_spec.md"}:
            sources.read_source(source["source_id"])
    structured = dict(
        protected=[o for rows in sources.protected.values() for o in rows],
        roles=sources.roles,
        request_fields=sources.request_fields,
    )
    sid = next(s["source_id"] for s in inv if s["filename"] == "report_request.docx")
    ref = dict(
        account_id=None,
        platform="Holloway",
        account_type="Stocks & Shares ISA",
        owner_text=None,
        reference_text="Holloway Stocks & Shares ISA",
    )
    candidate = dict(
        sources=[
            dict(source_id=s["source_id"], role="unknown", document_date=None)
            for s in inv
        ],
        observations=[
            dict(
                observation_type="report_scope",
                source_id=sid,
                source_text="Holloway Stocks & Shares ISA",
                sections=["introduction"],
                evidence_scope="client_specific",
                data=dict(clients=[], included_accounts=[ref], excluded_accounts=[]),
                field_states=[
                    dict(field="included_accounts[0].account_id", state="not_stated"),
                    dict(field="included_accounts[0].owner_text", state="not_stated"),
                ],
            )
        ],
        extraction_issues=[],
    )
    run = ExtractionRun(
        state="complete",
        response=candidate,
        inventory=sources.list_files()["sources"],
        structured=structured,
    )
    from agent_pipeline.extraction.results import ExtractionRecords

    extraction = ExtractionRecords(structured=structured).assemble(
        candidate, run.inventory, run.run_id
    )
    specification = (folder / "template_spec.md").read_text()
    facts = reconcile(extraction, specification=specification)
    return SimpleNamespace(
        folder=folder,
        run=run,
        facts=facts,
        specification=specification,
        output=tmp_path / "outputs",
    )


def test_writer_receives_only_reconciliation_and_code_preserves_values(inputs):
    client = FakeClient()
    report = write_report(client, "test", inputs.facts, config())
    payload = json.loads(client.requests[0]["messages"][1]["content"])
    assert set(payload) == {"reconciled_report"}
    assert "report_request" not in payload
    assert "£52,000.0" in report
    assert "GBP 20,000" in report
    assert "4 moderate" in report
    assert "## Tax Implications" not in report
    assert report.count(FCA_LINE) == 1
    assert "DRAFT — FOR HUMAN REVIEW" in report
    warning = "The value of investments can fall as well as rise"
    assert report.index(warning) < report.index("Please contact us")
    assert "subject to adviser review" in report
    assert len(client.requests) == 1


@pytest.mark.parametrize(
    "failure",
    ["invented_block", "omission", "duplicate", "section_order", "free_prose"],
)
def test_invalid_writer_arrangement_falls_back_without_changing_facts(inputs, failure):
    def change(result):
        if failure == "invented_block":
            result["sections"][0]["block_ids"] += ["£999999"]
        if failure == "omission":
            result["sections"][0]["block_ids"] = []
        if failure == "duplicate":
            result["sections"][0]["block_ids"] *= 2
        if failure == "section_order":
            result["sections"].reverse()
        if failure == "free_prose":
            result["sections"][0]["content"] = "Your gain is £999999."

    report = write_report(FakeClient(change), "test", inputs.facts, config())
    assert "£999999" not in report
    assert "£52,000.0" in report
    assert "writer arrangement could not be verified" in report


def test_pure_renderer_rejects_moved_blocks(inputs):
    plan = inputs.facts["report"]
    sections = [
        {"title": s["title"], "block_ids": list(s["block_ids"])}
        for s in plan["sections"]
        if s["include"]
    ]
    sections[1]["block_ids"].append(sections[0]["block_ids"].pop())
    with pytest.raises(WriterOutputError):
        render_report({"sections": sections}, inputs.facts)


def test_changed_headings_come_from_client_specification(inputs):
    spec = inputs.specification.replace("## Introduction", "## Your covered accounts")
    # The requirements, rather than hard-coded titles, control block placement.
    from agent_pipeline.reconciliation.report import build_report

    facts = deepcopy(inputs.facts)
    facts["report"] = build_report(facts, spec)
    report = write_report(FakeClient(), "test", facts, config())
    assert "## Your covered accounts" in report
    assert "## Introduction" not in report


def test_generate_reuses_extraction_not_current_raw_request_or_cached_reconciliation(
    inputs, monkeypatch
):
    folder = save_run(
        inputs.output, "example", inputs.run, ExtractionSettings(model="offline")
    )
    (folder / "reconciliation.json").write_text('{"fabricated": "£999999"}')
    (inputs.folder / "report_request.docx").unlink()
    client = FakeClient()
    monkeypatch.setattr(generate, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(generate, "load_environment", lambda: None)
    assert (
        generate.main(
            [
                "--client",
                "example",
                "--data-dir",
                str(inputs.folder.parent),
                "--run-dir",
                str(folder),
                "--model",
                "chosen-model",
            ]
        )
        == 0
    )
    assert "£52,000.0" in (folder / "report.md").read_text()
    assert "£999999" not in (folder / "report.md").read_text()
    assert client.requests[0]["model"] == "chosen-model"


def test_one_command_extracts_reconciles_and_writes(inputs, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(generate, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(generate, "load_environment", lambda: None)
    async_client = SimpleNamespace()

    async def close():
        pass

    async_client.close = close
    monkeypatch.setattr(generate, "AsyncOpenAI", lambda: async_client)

    class Extractor:
        def __init__(self, *args):
            pass

        async def run(self, folder):
            return inputs.run

    monkeypatch.setattr(generate, "ExtractionAgent", Extractor)
    assert (
        generate.main(
            [
                "--client",
                "example",
                "--data-dir",
                str(inputs.folder.parent),
                "--output-dir",
                str(inputs.output),
            ]
        )
        == 0
    )
    folder = inputs.output / "example" / inputs.run.run_id
    assert {
        "summary.json",
        "extraction.json",
        "validation.json",
        "reconciliation.json",
        "report.md",
    } <= {p.name for p in folder.iterdir()}


def test_mixed_client_run_is_rejected_before_api_call(inputs, monkeypatch):
    folder = save_run(
        inputs.output, "other-client", inputs.run, ExtractionSettings(model="offline")
    )
    client = FakeClient()
    monkeypatch.setattr(generate, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(generate, "load_environment", lambda: None)
    with pytest.raises(SystemExit):
        generate.main(
            [
                "--client",
                "example",
                "--data-dir",
                str(inputs.folder.parent),
                "--run-dir",
                str(folder),
            ]
        )
    assert client.requests == []


def test_writer_can_write_grounded_narrative_with_code_owned_values(inputs):
    def change(result):
        for section in result["sections"]:
            for bid in section["block_ids"]:
                block = inputs.facts["report"]["blocks"][bid]
                if block["text"].startswith("Recorded risk profile:"):
                    section["paragraphs"].append(
                        {
                            "block_id": bid,
                            "text": "Your recorded risk profile is {{value-1}} (moderate).",
                        }
                    )

    report = write_report(FakeClient(change), "test", inputs.facts, config())
    assert "Your recorded risk profile is 4 (moderate)." in report
    assert "writer arrangement could not be verified" not in report


@pytest.mark.parametrize(
    "text",
    [
        "Your risk profile is 99 (moderate).",
        "Your risk profile is moderate.",
        "Your risk profile is {{value-1}} with a guaranteed 10% return.",
    ],
)
def test_writer_cannot_change_add_or_drop_values(inputs, text):
    def change(result):
        for section in result["sections"]:
            for bid in section["block_ids"]:
                if inputs.facts["report"]["blocks"][bid]["text"].startswith(
                    "Recorded risk profile:"
                ):
                    section["paragraphs"].append({"block_id": bid, "text": text})

    report = write_report(FakeClient(change), "test", inputs.facts, config())
    assert "affected paragraphs retain the reconciled wording" in report
    assert "guaranteed" not in report
    assert "99" not in report


def test_natural_negation_is_allowed_and_locked_table_is_rendered_separately(inputs):
    from agent_pipeline.writing.writer import restore_values

    assert (
        restore_values(
            "The contribution of £20,000 does not involve selling investments.",
            "No investments are sold for the £20,000 contribution.",
        )
        == "The contribution of £20,000 does not involve selling investments."
    )

    def change(result):
        for section in result["sections"]:
            for bid in section["block_ids"]:
                if inputs.facts["report"]["blocks"][bid]["text"].startswith(
                    "| Account"
                ):
                    section["paragraphs"].append(
                        {"block_id": bid, "text": "No holdings."}
                    )

    report = write_report(FakeClient(change), "test", inputs.facts, config())
    assert "£52,000.0" in report
    assert "No holdings." not in report
    assert "writer arrangement could not be verified" not in report


def test_bad_paragraph_does_not_discard_other_accepted_writing(inputs):
    def change(result):
        for section in result["sections"]:
            for bid in section["block_ids"]:
                block = inputs.facts["report"]["blocks"][bid]
                if block["text"].startswith("Recorded risk profile:"):
                    section["paragraphs"].append(
                        {"block_id": bid, "text": "Your risk profile is 99."}
                    )
                if block["text"].startswith(
                    "Report instruction — product recommended:"
                ):
                    section["paragraphs"].append(
                        {
                            "block_id": bid,
                            "text": "We recommend topping up your existing Stocks & Shares ISA.",
                        }
                    )

    report = write_report(FakeClient(change), "test", inputs.facts, config())
    assert "We recommend topping up your existing Stocks & Shares ISA." in report
    assert "Recorded risk profile: 4 moderate." in report
    assert "99" not in report
    assert "Your platform charge has not been supplied" in report
    assert "Your ongoing advice charge has not been supplied" in report


def test_exact_supplied_values_are_accepted_and_model_sees_tokenised_input(inputs):
    def change(result):
        for section in result["sections"]:
            for bid in section["block_ids"]:
                if inputs.facts["report"]["blocks"][bid]["text"].startswith(
                    "Recorded risk profile:"
                ):
                    section["paragraphs"].append(
                        {
                            "block_id": bid,
                            "text": "Your agreed risk profile is 4 (moderate).",
                        }
                    )

    client = FakeClient(change)
    report = write_report(client, "test", inputs.facts, config())
    assert "Your agreed risk profile is 4 (moderate)." in report
    plan = json.loads(client.requests[0]["messages"][1]["content"])["reconciled_report"]
    assert any("{{value-1}}" in b.get("text", "") for b in plan["blocks"].values())
    assert all("text" not in b for b in plan["blocks"].values() if b["locked"])
