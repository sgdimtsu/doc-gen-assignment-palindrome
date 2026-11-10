"""Offline runtime checks, not report-quality evaluations."""
import asyncio
from copy import deepcopy
from openai.types.chat import ChatCompletion
import json
from types import SimpleNamespace
import pytest
from zipfile import ZipFile
from uuid import uuid4
from agent_pipeline import ROOT
from agent_pipeline.extraction.agent import ExtractionAgent, ExtractionSettings
from agent_pipeline.extraction.sources import SourceTools
from agent_pipeline.validation.status import validation_state


class Item:
    def __init__(self, **data):
        self.data = data

    def model_dump(self, **kwargs):
        return deepcopy(self.data)


def tool(name, **args):
    message = SimpleNamespace(
        content=None,
        refusal=None,
        tool_calls=[
            Item(
                id="call_" + uuid4().hex,
                type="function",
                function={"name": name, "arguments": json.dumps(args)},
            )
        ],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="tool_calls", message=message)],
        usage=None,
    )


def final(data, status="completed"):
    text = data if isinstance(data, str) else json.dumps(data)
    message = SimpleNamespace(content=text, refusal=None, tool_calls=None)
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop" if status == "completed" else "length",
                message=message,
            )
        ],
        usage=None,
    )


class FakeCompletions:
    def __init__(self, script):
        self.script = iter(script)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        response = next(self.script)
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return as_completion(response)

    @property
    def base_url(self):
        return "https://api.openai.com/v1/"

    def with_options(self, **kwargs):
        return self


def as_completion(response):
    choice = response.choices[0]
    message = choice.message
    usage = response.usage
    return ChatCompletion(
        id="test",
        created=0,
        model="offline-test",
        object="chat.completion",
        choices=[
            {
                "index": 0,
                "finish_reason": choice.finish_reason,
                "message": {
                    "role": "assistant",
                    "content": message.content,
                    "refusal": message.refusal,
                    "tool_calls": [c.model_dump() for c in message.tool_calls]
                    if message.tool_calls
                    else None,
                },
            }
        ],
        usage={
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.prompt_tokens + usage.completion_tokens,
        }
        if usage
        else None,
    )


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
        tool("read_source", source_id=case.ids["report_request.docx"]),
        tool("read_source", source_id=case.ids["template_spec.md"]),
    ]
    return case


def run_agent(case, script, **settings):
    fake = FakeCompletions(script)
    agent = ExtractionAgent(
        fake,
        ExtractionSettings(model="offline-test", **settings),
    )
    return (asyncio.run(agent.run(case.folder)), fake, agent)


def validated(candidate, completion=None):
    return [
        tool("validate_extraction", candidate=candidate),
        final(
            {
                "reviewed_source_ids": [s["source_id"] for s in candidate["sources"]],
                "findings": [],
            }
        ),
        completion or final(candidate),
    ]


def observation(case):
    return {
        "observation_type": "unclassified",
        "source_id": case.ids["template_spec.md"],
        "source_text": "Include background.",
        "sections": [],
        "evidence_scope": "client_specific",
        "data": {"reason_unclassified": "uncertain_relevance"},
        "field_states": [],
    }


def test_complete_run_passes_tools_and_strict_schema(case):
    data = deepcopy(case.data)
    data["observations"] = [observation(case)]
    (run, fake, _) = run_agent(case, case.reads + validated(data))
    assert run.state == "complete", run.errors
    assert run.tool_calls == 4
    assert fake.requests[0]["response_format"]["json_schema"]["strict"] is True
    assert {t["function"]["name"] for t in fake.requests[0]["tools"]} == {
        "list_files",
        "read_source",
    }
    assert "Cover the ISA." in json.dumps(fake.requests[-1]["messages"])


def test_partial_coverage_cannot_be_claimed_complete(case):
    (run, _, _) = run_agent(case, [final(case.data)])
    assert run.state == "partial"
    assert any("not delivered" in e for e in run.errors)


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
        tool("read_source", source_id=ids["report_request.docx"]),
        tool("read_source", source_id=ids["template_spec.md"]),
        tool("read_source", source_id=ids["screenshot.png"]),
    ]
    (run, fake, _) = run_agent(case, reads + validated(data))
    assert run.state == "complete", run.errors
    messages = fake.requests[-1]["messages"]
    assert any(
        (
            part.get("type") == "image_url"
            for m in messages
            if m.get("role") == "user" and isinstance(m["content"], list)
            for part in m["content"]
        )
    )
    for m in messages:
        if m.get("role") == "tool":
            assert "base64" not in m["content"]


def test_incomplete_model_response_is_partial(case):
    (run, _, _) = run_agent(case, case.reads + [final(case.data, status="incomplete")])
    assert run.state == "partial"


def test_missing_required_file_prevents_model_calls(case):
    (case.folder / "template_spec.md").unlink()
    (run, fake, _) = run_agent(case, [])
    assert run.state == "partial"
    assert not fake.requests


def test_limits_bound_execution(case):
    (run, _, _) = run_agent(case, [tool("list_files")], max_model_calls=1)
    assert run.state == "partial"
    assert any("MaxTurnsExceeded" in e for e in run.errors)
    (run, _, _) = run_agent(case, case.reads[:2], max_tool_calls=1)
    assert run.state == "partial"
    assert "Tool-call limit reached" in run.errors


def test_reported_unreadability_is_an_advisory_issue(case):
    data = deepcopy(case.data)
    obs = observation(case)
    data["extraction_issues"] = [
        {
            "issue_type": "unreadable_content",
            "source_id": obs["source_id"],
            "observation_indexes": [],
            "affected_fields": [],
            "description": "A region could not be read.",
        }
    ]
    (run, _, _) = run_agent(
        case, case.reads + [tool("validate_extraction", candidate=data), final(data)]
    )
    assert run.state == "complete"
    assert any("Reported incomplete" in e for e in run.validation_reports[-1]["errors"])


def test_authentication_error_records_safe_details(case):
    error = type("AuthenticationError", (Exception,), {})("sk-do-not-log-this")
    error.status_code = 401
    error.body = {"code": "invalid_api_key", "message": "sk-do-not-log-this"}
    error.request_id = "req_safe"
    run, fake, _ = run_agent(case, [error])
    assert run.state == "partial"
    assert len(fake.requests) == 1
    assert "API HTTP status: 401" in run.errors
    assert "sk-do-not-log-this" not in json.dumps(run.errors)


def test_chat_completion_request_and_tool_reply_ids(case):
    run, fake, _ = run_agent(case, case.reads + validated(case.data))
    assert run.state == "complete", run.errors
    request = fake.requests[-1]
    assert "input" not in request and "text" not in request
    assert request["messages"][0]["role"] == "system"
    assert request["max_tokens"] == 16000
    for definition in fake.requests[0]["tools"]:
        assert definition["type"] == "function"
        assert definition["function"]["strict"] is True
    pending = set()
    for message in request["messages"]:
        if message["role"] == "assistant":
            pending.update(call["id"] for call in message.get("tool_calls", []))
        elif message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
    assert pending == set()


def test_chat_completion_usage_is_recorded(case):
    completion = final(case.data)
    completion.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=25)
    run, _, _ = run_agent(case, case.reads + validated(case.data, completion))
    assert run.state == "complete", run.errors
    assert run.usage == {"input_tokens": 100, "output_tokens": 25}


def test_chat_completion_refusal_does_not_publish_complete_result(case):
    completion = final(case.data)
    completion.choices[0].message.refusal = "Cannot perform this extraction."
    run, _, _ = run_agent(case, [completion])
    assert run.state == "partial"
    assert any("Extraction stopped" in e for e in run.errors)


def test_agent_only_sees_listing_and_unified_reading_tools(case):
    run, fake, _ = run_agent(case, case.reads + validated(case.data))
    assert run.state == "complete", run.errors
    assert all(
        t["function"]["name"] in {"list_files", "read_source", "validate_extraction"}
        for request in fake.requests
        for t in request.get("tools", [])
    )


def test_json_document_reference_is_accepted(case):
    path = case.folder / "zz_accounts.json"
    path.write_text('{"accounts": [{"account_id": "A1", "value": null}]}')
    inventory = SourceTools(case.folder).list_files()["sources"]
    sid = next(s["source_id"] for s in inventory if s["filename"] == path.name)
    data = deepcopy(case.data)
    data["sources"].append(
        {"source_id": sid, "role": "account_records", "document_date": None}
    )
    obs = observation(case)
    obs["source_id"] = sid
    obs["source_text"] = '{"account_id": "A1", "value": null}'
    data["observations"] = [obs]
    run, _, _ = run_agent(
        case, case.reads + [tool("read_source", source_id=sid)] + validated(data)
    )
    assert run.state == "complete", run.errors


def test_unknown_document_reference_is_rejected(case):
    data = deepcopy(case.data)
    obs = observation(case)
    obs["source_id"] = "invented-source"
    data["observations"] = [obs]
    run, _, _ = run_agent(case, case.reads + validated(data))
    assert run.state == "partial"
    assert any(
        "unknown source ID" in error for error in run.validation_reports[-1]["errors"]
    )


def test_output_schema_requires_document_evidence_not_location(case):
    from jsonschema import ValidationError

    agent = ExtractionAgent(
        FakeCompletions([]), ExtractionSettings(model="offline-test")
    )
    data = deepcopy(case.data)
    data["observations"] = [observation(case)]
    agent.validator.validate(data)
    data["observations"][0]["location"] = {"paragraph": 1}
    with pytest.raises(ValidationError):
        agent.validator.validate(data)


def test_invalid_json_stops_without_custom_correction_loop(case):
    run, fake, _ = run_agent(case, case.reads + [final("{}")])
    assert run.state == "partial"
    assert run.last_output_text == "{}"
    assert len(fake.requests) == 4


def test_output_size_failure_cannot_count_as_complete_coverage(case):
    run, _, _ = run_agent(
        case, case.reads + [final(case.data)], max_tool_output_bytes=1
    )
    assert run.state == "partial"
    assert run.tool_errors


def test_invalid_read_order_can_be_recovered_within_budget(case):
    script = [tool("read_source", source_id=case.ids["template_spec.md"])]
    run, _, _ = run_agent(case, script + case.reads + validated(case.data))
    assert run.state == "complete", run.errors
    assert len(run.tool_errors) == 1


def test_repeated_invalid_arguments_stop_the_run(case):
    script = [tool("read_source", source_id="invented")] * 3
    # Each invocation needs its own call ID, as required by the SDK.
    script = [tool("read_source", source_id="invented") for _ in script]
    run, fake, _ = run_agent(case, script)
    assert run.state == "partial"
    assert len(fake.requests) == 3
    assert len(run.tool_errors) == 3


def review(candidate, findings=None):
    return final(
        {
            "reviewed_source_ids": [s["source_id"] for s in candidate["sources"]],
            "findings": findings or [],
        }
    )


def omission(case, severity="error"):
    return {
        "category": "omission",
        "severity": severity,
        "message": "Background requirement was omitted.",
        "observation_ids": [],
        "source_ids": [case.ids["template_spec.md"]],
        "evidence": [
            {
                "source_id": case.ids["template_spec.md"],
                "source_text": "Include background.",
            }
        ],
    }


def test_unreviewed_extraction_is_not_falsely_approved(case):
    run, _, _ = run_agent(case, case.reads + [final(case.data)])
    assert run.state == "complete"
    assert validation_state(run.response, run.validation_reports) == "not_reviewed"


def test_changed_candidate_is_marked_stale_without_blocking(case):
    changed = deepcopy(case.data)
    changed["sources"][0]["document_date"] = "2026-01-01"
    script = validated(case.data)[:-1] + [final(changed)]
    run, _, _ = run_agent(case, case.reads + script)
    assert run.validation_reports[-1]["state"] == "passed"
    assert run.state == "complete"
    assert validation_state(run.response, run.validation_reports) == "stale"


def test_reviewer_failure_can_be_repaired_and_validated_again(case):
    repaired = deepcopy(case.data)
    repaired["observations"] = [observation(case)]
    script = [
        tool("validate_extraction", candidate=case.data),
        review(case.data, [omission(case)]),
    ] + validated(repaired)
    run, fake, _ = run_agent(case, case.reads + script)
    assert run.state == "complete", run.errors
    assert [r["state"] for r in run.validation_reports] == ["failed", "passed"]
    review_requests = [
        r
        for r in fake.requests
        if r["response_format"]["json_schema"]["name"] == "extraction_review"
    ]
    assert len(review_requests) == 2
    # Reviewer sees evidence + candidate, without the extractor's conversation or tools.
    assert all(len(r["messages"]) == 2 and "tools" not in r for r in review_requests)
    assert all(
        "Include background." in json.dumps(r["messages"]) for r in review_requests
    )


def test_unresolved_review_errors_are_preserved_without_blocking(case):
    script = [
        tool("validate_extraction", candidate=case.data),
        review(case.data, [omission(case)]),
        final(case.data),
    ]
    run, _, _ = run_agent(case, case.reads + script)
    assert run.validation_reports[-1]["review_findings"][0]["category"] == "omission"
    assert run.state == "complete"
    assert validation_state(run.response, run.validation_reports) == "failed"


def test_clean_llm_review_cannot_overrule_deterministic_errors(case):
    candidate = deepcopy(case.data)
    candidate["observations"] = [observation(case)]
    candidate["observations"][0]["source_text"] = "Invented quotation."
    run, _, _ = run_agent(case, case.reads + validated(candidate))
    assert run.state == "complete"
    assert validation_state(run.response, run.validation_reports) == "failed"
    assert run.validation_reports[-1]["review_findings"] == []
    assert (
        run.validation_reports[-1]["deterministic_findings"][0]["category"] == "quote"
    )


def test_validation_budget_ends_repairs_not_extraction(case):
    script = [
        tool("validate_extraction", candidate=case.data),
        review(case.data, [omission(case)]),
        tool("validate_extraction", candidate=case.data),
        final(case.data),
    ]
    run, fake, _ = run_agent(case, case.reads + script, max_validation_attempts=1)
    assert len(run.validation_reports) == 1
    assert run.errors == []
    assert run.state == "complete"
    assert len(fake.requests) == 7


def test_incomplete_evidence_review_cannot_pass(case):
    script = [
        tool("validate_extraction", candidate=case.data),
        final({"reviewed_source_ids": [], "findings": []}),
        final(case.data),
    ]
    run, _, _ = run_agent(case, case.reads + script)
    assert run.state == "complete"
    assert validation_state(run.response, run.validation_reports) == "incomplete"
    assert run.validation_reports[-1]["evidence_review_complete"] is False


def test_warning_is_preserved_without_blocking_release(case):
    script = [
        tool("validate_extraction", candidate=case.data),
        review(case.data, [omission(case, severity="warning")]),
        final(case.data),
    ]
    run, _, _ = run_agent(case, case.reads + script)
    assert run.state == "complete", run.errors
    assert run.validation_reports[-1]["review_findings"][0]["severity"] == "warning"


def test_validation_is_only_exposed_after_all_sources_are_delivered(case):
    run, fake, _ = run_agent(case, case.reads + validated(case.data))
    assert run.state == "complete", run.errors
    requests = [r for r in fake.requests if "tools" in r]
    for request in requests[: len(case.reads)]:
        assert "validate_extraction" not in {
            t["function"]["name"] for t in request["tools"]
        }
    assert "validate_extraction" in {
        t["function"]["name"] for t in requests[len(case.reads)]["tools"]
    }


@pytest.mark.parametrize(
    "status,delivered",
    [
        ("pending", True),
        ("partial", True),
        ("failed", True),
        ("unsupported", True),
        ("read", False),
    ],
)
def test_unready_sources_block_validation_without_spending_attempts(
    case, status, delivered
):
    from agent_pipeline.extraction.models import ExtractionRun
    from agent_pipeline.validation.tool import CandidateValidationTool

    sources = SourceTools(case.folder)
    for source in sources.sources.values():
        source["read_status"] = "read"
    sid = next(iter(sources.sources))
    sources.sources[sid]["read_status"] = status
    delivered_ids = set(sources.sources)
    if not delivered:
        delivered_ids.remove(sid)
    run = ExtractionRun()
    validation = CandidateValidationTool(
        FakeCompletions([]),
        ExtractionSettings(model="offline-test"),
        ROOT / "config/extraction_schema.json",
        sources,
        run,
        delivered_ids,
    )
    tool_definition = validation.definition()
    assert tool_definition.is_enabled(None, None) is False
    result = json.loads(
        asyncio.run(validation.invoke(None, json.dumps({"candidate": case.data})))
    )
    assert result["state"] == "blocked"
    assert result["attempts_remaining"] == 3
    assert run.validation_reports == []


def test_rejected_review_is_saved_but_not_sent_as_repair_findings(case):
    rejected = {"reviewed_source_ids": [], "findings": []}
    script = [
        tool("validate_extraction", candidate=case.data),
        final(rejected),
        final(case.data),
    ]
    run, fake, _ = run_agent(case, case.reads + script)
    assert run.state == "complete"
    assert run.validation_reports[-1]["review_diagnostics"]["response"] == rejected
    feedback = json.loads(
        [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    )
    assert set(feedback) == {
        "state",
        "evidence_review_complete",
        "deterministic_findings",
        "review_findings",
        "errors",
        "attempts_remaining",
    }
    assert feedback["review_findings"] == []
    assert "missing=" in feedback["errors"][0]
