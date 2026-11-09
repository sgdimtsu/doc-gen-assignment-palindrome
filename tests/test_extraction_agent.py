"""Offline runtime checks, not report-quality evaluations."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from zipfile import ZipFile
from agent_pipeline.extraction_agent import ExtractionAgent, ExtractionSettings, ROOT
from agent_pipeline.source_tools import SourceTools


class Item:
    def __init__(self, **data):
        self.data = data

    def model_dump(self, **kwargs):
        return deepcopy(self.data)


def tool(name, **args):
    return SimpleNamespace(
        status="completed",
        output=[
            Item(
                type="function_call",
                name=name,
                arguments=json.dumps(args),
                call_id=name,
            )
        ],
        output_text="",
        usage=None,
    )


def final(data, status="completed"):
    text = data if isinstance(data, str) else json.dumps(data)
    return SimpleNamespace(
        status=status,
        output=[Item(type="message", content=[{"type": "output_text", "text": text}])],
        output_text=text,
        usage=None,
    )


class FakeResponses:
    def __init__(self, script):
        self.script = iter(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        response = next(self.script)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def case(tmp_path):
    case = SimpleNamespace()
    case.folder = tmp_path
    with ZipFile(case.folder / "report_request.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Cover the ISA.</w:t></w:r></w:p></w:body></w:document>",
        )
    (case.folder / "template_spec.md").write_text("Include background.")
    case.inventory = SourceTools(case.folder).list_files()["sources"]
    case.ids = {s["filename"]: s["source_id"] for s in case.inventory}
    case.data = {
        "sources": [
            {
                "source_id": s["source_id"],
                "role": "report_request"
                if s["filename"].endswith(".docx")
                else "report_specification",
                "document_date": None,
            }
            for s in case.inventory
        ],
        "observations": [],
        "extraction_issues": [],
    }
    case.reads = [
        tool("list_files"),
        tool("read_document", source_id=case.ids["report_request.docx"]),
        tool("read_document", source_id=case.ids["template_spec.md"]),
    ]
    return case


def run_agent(case, script, **settings):
    fake = FakeResponses(script)
    agent = ExtractionAgent(
        SimpleNamespace(responses=fake),
        ExtractionSettings(model="offline-test", retry_delay_seconds=0, **settings),
    )
    return (agent.run(case.folder), fake, agent)


def observation(case):
    schema = json.loads((ROOT / "config/extraction_schema.json").read_text())

    def make(node):
        if "$ref" in node:
            return make(schema["$defs"][node["$ref"].split("/")[-1]])
        if "anyOf" in node:
            if {"type": "null"} in node["anyOf"]:
                return None
            return make(node["anyOf"][0])
        if "enum" in node:
            return node["enum"][0]
        if node["type"] == "object":
            return {k: make(v) for (k, v) in node["properties"].items()}
        return {"array": [], "string": "text", "integer": 0}[node["type"]]

    obs = make(schema["$defs"]["unclassified_observation"])
    obs["source_id"] = case.ids["template_spec.md"]
    obs["location"]["line"] = 1
    obs["source_text"] = "Include background."
    return obs


def test_complete_run_passes_tools_and_strict_schema(case):
    data = deepcopy(case.data)
    data["observations"] = [observation(case)]
    (run, fake, _) = run_agent(case, case.reads + [final(data)])
    assert run.state == "complete", run.errors
    assert run.tool_calls == 3
    assert fake.requests[0]["text"]["format"]["strict"] is True
    assert {t["name"] for t in fake.requests[0]["tools"]} == {
        "list_files",
        "read_document",
        "read_json",
        "inspect_image",
    }
    assert "Cover the ISA." in json.dumps(fake.requests[-1]["input"])


def test_early_final_is_corrected_after_reading(case):
    (run, _, _) = run_agent(case, [final(case.data)] + case.reads + [final(case.data)])
    assert run.state == "complete", run.errors


def test_partial_coverage_cannot_be_claimed_complete(case):
    (run, _, _) = run_agent(case, [final(case.data)], correction_attempts=0)
    assert run.state == "partial"
    assert any(("not delivered" in e for e in run.errors))


def test_invalid_schema_can_be_corrected_once(case):
    (run, _, _) = run_agent(case, case.reads + [final("{}"), final(case.data)])
    assert run.state == "complete", run.errors


def test_unknown_source_or_location_is_rejected(case):
    for field, value in [("source_id", "invented"), ("location", {"line": 999})]:
        data = deepcopy(case.data)
        obs = observation(case)
        if field == "location":
            obs["location"]["line"] = value["line"]
        else:
            obs[field] = value
        data["observations"] = [obs]
        (run, _, _) = run_agent(case, case.reads + [final(data)], correction_attempts=0)
        assert run.state == "partial"


def test_image_is_delivered_as_image_not_text(case):
    image = ROOT / "data/client_02_medium/statement_summary.png"
    (case.folder / "screenshot.png").write_bytes(image.read_bytes())
    inventory = SourceTools(case.folder).list_files()["sources"]
    ids = {s["filename"]: s["source_id"] for s in inventory}
    data = deepcopy(case.data)
    data["sources"] = [
        {"source_id": s["source_id"], "role": "unknown", "document_date": None}
        for s in inventory
    ]
    reads = [
        tool("list_files"),
        tool("read_document", source_id=ids["report_request.docx"]),
        tool("read_document", source_id=ids["template_spec.md"]),
        tool("inspect_image", source_id=ids["screenshot.png"]),
    ]
    (run, fake, _) = run_agent(case, reads + [final(data)])
    assert run.state == "complete", run.errors
    messages = fake.requests[-1]["input"]
    assert any(
        (
            part.get("type") == "input_image"
            for m in messages
            if m.get("role") == "user" and isinstance(m["content"], list)
            for part in m["content"]
        )
    )
    for m in messages:
        if m.get("type") == "function_call_output":
            assert "base64" not in m["output"]


def test_incomplete_model_response_is_partial(case):
    (run, _, _) = run_agent(case, case.reads + [final(case.data, status="incomplete")])
    assert run.state == "partial"


def test_retry_only_transient_errors(case):
    transient = type("TransientError", (Exception,), {"status_code": 429})()
    (run, _, _) = run_agent(case, [transient] + case.reads + [final(case.data)])
    assert run.state == "complete", run.errors
    (run, fake, _) = run_agent(case, [ValueError("permanent")])
    assert run.state == "partial"
    assert len(fake.requests) == 1


def test_missing_required_file_prevents_model_calls(case):
    (case.folder / "template_spec.md").unlink()
    (run, fake, _) = run_agent(case, [])
    assert run.state == "partial"
    assert not fake.requests


def test_limits_bound_execution(case):
    (run, _, _) = run_agent(case, [tool("list_files")], max_model_calls=1)
    assert run.state == "partial"
    assert "Model-call limit reached" in run.errors
    (run, _, _) = run_agent(case, case.reads[:2], max_tool_calls=1)
    assert run.state == "partial"
    assert "Tool-call limit reached" in run.errors


def test_reported_unreadability_is_not_complete(case):
    data = deepcopy(case.data)
    obs = observation(case)
    data["extraction_issues"] = [
        {
            "issue_type": "unreadable_content",
            "source_id": obs["source_id"],
            "location": obs["location"],
            "observation_indexes": [],
            "affected_fields": [],
            "description": "A region could not be read.",
        }
    ]
    (run, _, _) = run_agent(case, case.reads + [final(data)])
    assert run.state == "partial"
    assert any(("Reported incomplete" in e for e in run.errors))
