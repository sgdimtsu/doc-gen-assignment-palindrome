from copy import deepcopy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from agent_pipeline import extract
from agent_pipeline.extraction_agent import ROOT
from agent_pipeline.source_tools import SourceTools


class OutputItem:
    def __init__(self, data):
        self.data = data

    def model_dump(self, **kwargs):
        return deepcopy(self.data)


class ScriptedClient:
    def __init__(self, client_dir, *, final_status="completed"):
        inventory = SourceTools(client_dir).list_files()["sources"]
        ids = {s["filename"]: s["source_id"] for s in inventory}
        ordered = [ids["report_request.docx"], ids["template_spec.md"]]
        ordered += [s["source_id"] for s in inventory if s["source_id"] not in ordered]
        formats = {s["source_id"]: s["format"] for s in inventory}
        self.calls = [("list_files", {})]
        self.calls += [
            (
                "read_json" if formats[sid] == "json" else "read_document",
                {"source_id": sid},
            )
            for sid in ordered
        ]
        self.data = {
            "sources": [
                {"source_id": s["source_id"], "role": "unknown", "document_date": None}
                for s in inventory
            ],
            "observations": [],
            "extraction_issues": [],
        }
        self.requests = []
        self.responses = self
        self.closed = False
        self.final_status = final_status

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        index = len(self.requests)
        self.requests.append(kwargs)
        if index < len(self.calls):
            name, args = self.calls[index]
            data = {
                "type": "function_call",
                "call_id": f"call-{index}",
                "name": name,
                "arguments": json.dumps(args),
            }
            return SimpleNamespace(
                status="completed",
                output=[OutputItem(data)],
                output_text="",
                usage=None,
            )
        text = json.dumps(self.data)
        return SimpleNamespace(
            status=self.final_status,
            output=[
                OutputItem(
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            ],
            output_text=text,
            usage=None,
        )

    def close(self):
        self.closed = True


@pytest.fixture
def cli_case(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    client_dir = data_dir / "example"
    shutil.copytree(ROOT / "data/client_01_clean", client_dir)
    output_dir = tmp_path / "outputs"
    client = ScriptedClient(client_dir)
    monkeypatch.setattr(extract, "_create_client", lambda: client)
    monkeypatch.setattr(extract, "_load_environment", lambda path: None)
    monkeypatch.delenv("EXTRACTION_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    return SimpleNamespace(
        client=client,
        client_dir=client_dir,
        output_dir=output_dir,
        argv=[
            "--client",
            "example",
            "--data-dir",
            str(data_dir),
            "--output-dir",
            str(output_dir),
        ],
    )


def test_cli_runs_real_tools_agent_and_saver_with_fake_api(cli_case, capsys):
    assert extract.main(cli_case.argv) == 0
    root = cli_case.output_dir / "example"
    result = json.loads((root / "extraction.json").read_text())
    manifest = json.loads((root / "extraction_manifest.json").read_text())
    assert manifest["state"] == "complete"
    assert len(result["sources"]) == 6
    assert all(s["read_status"] == "read" for s in result["sources"])
    assert cli_case.client.closed
    assert not (cli_case.output_dir / "example.md").exists()
    assert "Extraction: complete" in capsys.readouterr().out


def test_cli_partial_run_returns_nonzero_and_saves_manifest(cli_case):
    cli_case.client.final_status = "incomplete"
    assert extract.main(cli_case.argv) == 1
    root = cli_case.output_dir / "example"
    assert (
        json.loads((root / "extraction_manifest.json").read_text())["state"]
        == "partial"
    )
    assert not (root / "extraction.json").exists()


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
    argv = cli_case.argv + (["--model", explicit] if explicit else [])
    assert extract.main(argv) == 0
    assert cli_case.client.requests[0]["model"] == expected


def test_limits_are_passed_to_agent_and_saved(cli_case):
    assert (
        extract.main(
            cli_case.argv + ["--max-model-calls", "1", "--max-tool-calls", "3"]
        )
        == 1
    )
    manifest = json.loads(
        (cli_case.output_dir / "example/extraction_manifest.json").read_text()
    )
    assert manifest["settings"]["max_model_calls"] == 1
    assert manifest["settings"]["max_tool_calls"] == 3
    assert manifest["model_calls"] == 1


@pytest.mark.parametrize(
    "extra", [["--max-model-calls", "0"], ["--client", "../outside"]]
)
def test_invalid_arguments_do_not_call_model(cli_case, extra):
    with pytest.raises(SystemExit) as error:
        extract.main(cli_case.argv + extra)
    assert error.value.code == 2
    assert cli_case.client.requests == []


def test_missing_client_is_argument_error(cli_case):
    with pytest.raises(SystemExit) as error:
        extract.main(cli_case.argv + ["--client", "missing"])
    assert error.value.code == 2
    assert cli_case.client.requests == []


def test_client_initialisation_error_does_not_print_secret(
    cli_case, monkeypatch, capsys
):
    def fail():
        raise ValueError("secret-api-key-must-not-be-printed")

    monkeypatch.setattr(extract, "_create_client", fail)
    assert extract.main(cli_case.argv) == 1
    assert "secret-api-key" not in capsys.readouterr().err
