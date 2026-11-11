from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest
from agents.strict_schema import ensure_strict_json_schema

from agent_pipeline import ROOT
from agent_pipeline.evaluation import evaluate as ev


def judgment():
    return {
        "reviewed_sources": ["notes.md"],
        "criteria": {
            name: {
                "rating": "meets",
                "explanation": "Supported by the supplied evidence.",
                "report_excerpt": "Recorded fact.",
                "source_evidence": [
                    {"filename": "notes.md", "source_excerpt": "Recorded fact."}
                ],
            }
            for name in ev.GROUND + ev.QUALITY
        },
        "findings": [],
    }


@pytest.mark.parametrize(
    "rating, expected",
    [
        ("meets", "pass"),
        ("partly_meets", "needs_review"),
        ("does_not_meet", "needs_review"),
        ("unable_to_assess", "needs_review"),
    ],
)
def test_quality_cannot_hide_or_create_critical_factual_failure(rating, expected):
    result = judgment()
    result["criteria"]["readability_clarity"]["rating"] = rating
    checks = [dict(passed=True, severity="requirement")]
    assert ev.overall_outcome(checks, result, []) == expected
    result["findings"] = [dict(criterion="factual_correctness", severity="critical")]
    assert ev.overall_outcome(checks, result, []) == "fail"


def test_incomplete_or_rejected_judge_cannot_pass():
    assert ev.overall_outcome([], None, ["API failed"]) == "needs_review"
    assert (
        ev.overall_outcome([], judgment(), ["Unsupported citation"]) == "needs_review"
    )
    assert (
        ev.overall_outcome(
            [dict(passed=False, severity="requirement")], None, ["API failed"]
        )
        == "fail"
    )
    assert (
        ev.overall_outcome(
            [dict(passed=False, severity="presentation")], judgment(), []
        )
        == "needs_review"
    )


def test_evidence_and_coverage_checks_reject_invented_references():
    sources = [dict(filename="notes.md", text="Recorded fact.", image=None)]
    result = judgment()
    assert ev.validate_judgment(result, "Recorded fact.", sources) == []
    result["reviewed_sources"] = []
    result["criteria"]["factual_correctness"]["source_evidence"][0][
        "source_excerpt"
    ] = "Invented evidence."
    errors = ev.validate_judgment(result, "Recorded fact.", sources)
    assert any("every source" in e for e in errors)
    assert any("source excerpt" in e for e in errors)


def test_ground_truth_failure_requires_evidence_backed_finding():
    result = judgment()
    result["criteria"]["factual_correctness"]["rating"] = "does_not_meet"
    assert any(
        "lacks a finding" in e
        for e in ev.validate_judgment(
            result,
            "Recorded fact.",
            [dict(filename="notes.md", text="Recorded fact.", image=None)],
        )
    )


def test_code_checks_exact_requirements_without_formatting_financial_numbers():
    spec = """## Scope
Keep the FCA authorisation sentence as written.
## Holdings
Table columns Account | Owner | Type | Value.
## Tax
Include ONLY if a disposal is recommended.
## Closing
Must include this warning, word for word: "Required warning."
"""
    report = """## Scope
This firm is authorised and regulated by the Financial Conduct Authority.
## Holdings
| Account | Owner | Type | Value |
|---|---|---|---|
| ISA | Alex | ISA | £1,000 |
## Closing
Required warning.
"""
    checks = ev.code_checks(report, spec, dict(tax_section_required=False))
    assert all(c["passed"] for c in checks)
    for changed in [
        report.replace("Required warning.", "Paraphrased warning."),
        report.replace("## Closing", "## Tax\n## Closing"),
        report.replace("| Value |", "| Balance |"),
    ]:
        assert any(
            c["passed"] is False
            for c in ev.code_checks(changed, spec, dict(tax_section_required=False))
        )
    assert ensure_strict_json_schema(ev.judge_schema(["notes.md"]))


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def create(self, **kwargs):
        self.requests.append(kwargs)
        payload = json.loads(kwargs["messages"][1]["content"][0]["text"])
        result = deepcopy(self.reply)
        result["reviewed_sources"] = [s["filename"] for s in payload["sources"]]
        for criterion in result["criteria"].values():
            criterion["source_evidence"] = []
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(result), refusal=None),
                )
            ]
        )


def test_command_uses_original_sources_once_and_saves_uncalibrated_result(
    tmp_path, monkeypatch
):
    client_dir = tmp_path / "data" / "client_01_clean"
    shutil.copytree(ROOT / "data/client_01_clean", client_dir)
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text('{"client":"client_01_clean"}')
    (run / "report.md").write_text("Recorded fact.")
    (run / "reconciliation.json").write_text('{"wrong_answer":"DO NOT SEND THIS"}')
    client = FakeClient(judgment())
    monkeypatch.setattr(ev, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(ev, "load_environment", lambda: None)
    code = ev.main(
        [
            "--client",
            "client_01_clean",
            "--run-dir",
            str(run),
            "--data-dir",
            str(client_dir.parent),
        ]
    )
    assert code == 2  # Fixed required structure/wording is missing.
    saved = json.loads((run / "evaluation.json").read_text())
    assert saved["outcome"] == "fail"
    assert "no human review" in saved["limitation"]
    assert len(client.requests) == 1
    payload = json.loads(client.requests[0]["messages"][1]["content"][0]["text"])
    assert "DO NOT SEND THIS" not in json.dumps(payload)
    assert {
        "report_request.docx",
        "template_spec.md",
        "meeting_notes.docx",
        "client_data_db.json",
    } <= {s["filename"] for s in payload["sources"]}
    assert payload["expected_outcomes"]


def test_wrong_client_is_rejected_before_call(tmp_path, monkeypatch):
    (tmp_path / "summary.json").write_text('{"client":"wrong-client"}')
    monkeypatch.setattr(
        ev, "OpenAI", lambda **kwargs: pytest.fail("No API call expected")
    )
    with pytest.raises(SystemExit):
        ev.main(["--client", "client_01_clean", "--run-dir", str(tmp_path)])


def test_images_reach_evaluator_as_images_not_filename_markers():
    sources, errors = ev.load_sources(ROOT / "data/client_02_medium")
    assert not errors
    image = next(s for s in sources if s["filename"].endswith(".png"))
    assert image["image"]["image_url"].startswith("data:image/png;base64,")


def test_non_ground_truth_findings_cannot_be_critical_financial_failures():
    result = judgment()
    result["criteria"]["tone_presentation"]["rating"] = "does_not_meet"
    result["findings"] = [
        dict(
            criterion="tone_presentation",
            severity="critical",
            explanation="Jargon.",
            report_excerpt="Recorded fact.",
            source_evidence=[],
        )
    ]
    errors = ev.validate_judgment(
        result,
        "Recorded fact.",
        [dict(filename="notes.md", text="Recorded fact.", image=None)],
    )
    assert errors
    assert ev.overall_outcome([], result, errors) == "needs_review"
