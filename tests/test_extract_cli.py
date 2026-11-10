import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from agent_pipeline import extract
from agent_pipeline import ROOT
from agent_pipeline.extraction.sources import SourceTools


class ScriptedClient:
    def __init__(self, client_dir, *, final_status="completed"):
        inventory = SourceTools(client_dir).list_files()["sources"]
        ids = {s["filename"]: s["source_id"] for s in inventory}
        ordered = [ids["report_request.docx"], ids["template_spec.md"]]
        ordered += [s["source_id"] for s in inventory if s["source_id"] not in ordered]
        self.data = {
            "sources": [
                {"source_id": s["source_id"], "role": "unknown", "document_date": None}
                for s in inventory
            ],
            "observations": [],
            "extraction_issues": [],
        }
        self.calls = iter(
            [("list_files", {})]
            + [("read_source", {"source_id": sid}) for sid in ordered]
            + [("validate_extraction", {"candidate": self.data})]
        )
        self.requests = []
        self.chat = SimpleNamespace(completions=self)
        self.closed = False
        self.final_status = final_status

    def with_options(self, **kwargs):
        return self

    @property
    def base_url(self):
        return "https://api.openai.com/v1/"

    async def create(self, **kwargs):
        from openai.types.chat import ChatCompletion

        self.requests.append(kwargs)
        message = {"role": "assistant", "content": None}
        if kwargs["response_format"]["json_schema"]["name"] == "extraction_review":
            message["content"] = json.dumps(
                {
                    "reviewed_source_ids": [
                        s["source_id"] for s in self.data["sources"]
                    ],
                    "findings": [],
                }
            )
            finish = "stop"
        else:
            call = next(self.calls, None)
            if call:
                name, args = call
                message["tool_calls"] = [
                    {
                        "id": f"call-{len(self.requests)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ]
                finish = "tool_calls"
            else:
                message["content"] = json.dumps(self.data)
                finish = "stop" if self.final_status == "completed" else "length"
        return ChatCompletion(
            id="test",
            created=0,
            model="offline",
            object="chat.completion",
            choices=[{"index": 0, "finish_reason": finish, "message": message}],
        )

    async def close(self):
        self.closed = True


@pytest.fixture
def cli_case(tmp_path, monkeypatch):
    client_dir = tmp_path / "data" / "example"
    shutil.copytree(ROOT / "data/client_01_clean", client_dir)
    client = ScriptedClient(client_dir)
    monkeypatch.setattr(extract, "ROOT", tmp_path)
    monkeypatch.setattr(extract, "_create_client", lambda: client)
    monkeypatch.setattr(extract, "load_environment", lambda: None)
    monkeypatch.delenv("EXTRACTION_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    return SimpleNamespace(
        client=client,
        output_dir=tmp_path / "outputs",
        argv=["--client", "example", "--output-dir", str(tmp_path / "outputs")],
    )


def run_folder(case):
    return next((case.output_dir / "example").iterdir())


def test_cli_runs_real_agent_validation_and_saving(cli_case, capsys):
    assert extract.main(cli_case.argv) == 0
    folder = run_folder(cli_case)
    result = json.loads((folder / "extraction.json").read_text())
    assert len(result["sources"]) == 6
    assert all(s["read_status"] == "read" for s in result["sources"])
    assert cli_case.client.closed
    assert "Extraction: complete" in capsys.readouterr().out
    assert json.loads((folder / "validation.json").read_text())[-1]["state"] == "passed"


def test_cli_partial_run_returns_nonzero_and_only_partial_output(cli_case):
    cli_case.client.final_status = "incomplete"
    assert extract.main(cli_case.argv) == 1
    folder = run_folder(cli_case)
    assert json.loads((folder / "summary.json").read_text())["state"] == "partial"
    assert not (folder / "extraction.json").exists()


@pytest.mark.parametrize(
    "explicit,extraction_env,baseline_env,expected",
    [
        (None, None, None, "gpt-4o-mini"),
        (None, None, "baseline", "baseline"),
        (None, "extraction", "baseline", "extraction"),
        ("explicit", "extraction", "baseline", "explicit"),
    ],
)
def test_model_precedence(
    cli_case, monkeypatch, explicit, extraction_env, baseline_env, expected
):
    if extraction_env:
        monkeypatch.setenv("EXTRACTION_MODEL", extraction_env)
    if baseline_env:
        monkeypatch.setenv("OPENAI_MODEL", baseline_env)
    assert (
        extract.main(cli_case.argv + (["--model", explicit] if explicit else [])) == 0
    )
    assert cli_case.client.requests[0]["model"] == expected


def test_execution_limits_are_settings_not_cli_options(cli_case, monkeypatch):
    from agent_pipeline.extraction.models import ExtractionSettings

    monkeypatch.setattr(
        extract,
        "ExtractionSettings",
        lambda model: ExtractionSettings(model=model, max_model_calls=1),
    )
    assert extract.main(cli_case.argv) == 1
    summary = json.loads((run_folder(cli_case) / "summary.json").read_text())
    assert summary["model_calls"] == 1
    assert summary["settings"]["max_model_calls"] == 1


@pytest.mark.parametrize("name", ["../outside", "missing"])
def test_invalid_client_is_argument_error(cli_case, name):
    with pytest.raises(SystemExit) as error:
        extract.main(cli_case.argv + ["--client", name])
    assert error.value.code == 2
    assert cli_case.client.requests == []


def test_initialisation_error_does_not_print_secret(cli_case, monkeypatch, capsys):
    def fail():
        raise ValueError("secret-api-key-must-not-be-printed")

    monkeypatch.setattr(extract, "_create_client", fail)
    assert extract.main(cli_case.argv) == 1
    assert "secret-api-key" not in capsys.readouterr().err


def test_api_failure_keeps_status_without_raw_body(cli_case, monkeypatch, capsys):
    async def fail(**kwargs):
        error = type("AuthenticationError", (Exception,), {})("sk-do-not-log-this")
        error.status_code = 401
        error.body = {"message": "sk-do-not-log-this"}
        raise error

    monkeypatch.setattr(cli_case.client, "create", fail)
    assert extract.main(cli_case.argv) == 1
    errors = capsys.readouterr().err
    summary = (run_folder(cli_case) / "summary.json").read_text()
    assert "401" in errors and "401" in summary
    assert "sk-do-not-log-this" not in errors + summary
