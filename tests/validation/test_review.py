import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from jsonschema import Draft202012Validator, ValidationError

from agent_pipeline.extraction.sources import SourceTools
from agent_pipeline.validation.review import ExtractionValidator


@pytest.fixture
def review_case(tmp_path):
    with ZipFile(tmp_path / "report_request.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Cover the ISA.</w:t></w:r></w:p></w:body></w:document>",
        )
    (tmp_path / "template_spec.md").write_text("Include background.")
    inventory = SourceTools(tmp_path).list_files()["sources"]
    extraction = {
        "sources": [
            {**s, "read_status": "read", "role": "unknown", "document_date": None}
            for s in inventory
        ],
        "observations": [],
        "extraction_issues": [],
    }
    ids = [s["source_id"] for s in inventory]
    return SimpleNamespace(
        folder=tmp_path,
        extraction=extraction,
        ids=ids,
        clean={"reviewed_source_ids": ids, "findings": []},
    )


class ReviewClient:
    def __init__(self, output):
        self.output = output
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **kwargs):
        return self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=self.output
                        if isinstance(self.output, str)
                        else json.dumps(self.output),
                        refusal=None,
                    ),
                )
            ],
        )


def validate(case, response):
    client = ReviewClient(response)
    report = asyncio.run(
        ExtractionValidator().validate(case.extraction, case.folder, client)
    )
    return report, client


def test_rules_only_review_cannot_claim_evidence_reviewed_pass(review_case):
    before = deepcopy(review_case.extraction)
    report = asyncio.run(
        ExtractionValidator().validate(review_case.extraction, review_case.folder)
    )
    assert report["state"] == "not_reviewed"
    assert report["evidence_review_complete"] is False
    assert report["errors"] == []
    assert review_case.extraction == before


def test_complete_review_passes_and_request_constrains_inventory(review_case):
    report, client = validate(review_case, review_case.clean)
    assert report["state"] == "passed"
    assert report["evidence_review_complete"] is True
    request = client.requests[0]
    schema = request["response_format"]["json_schema"]["schema"]
    ids_schema = schema["properties"]["reviewed_source_ids"]
    assert ids_schema["items"]["enum"] == review_case.ids
    assert ids_schema["minItems"] == ids_schema["maxItems"] == 2
    payload = json.loads(request["messages"][1]["content"][0]["text"])
    assert payload["expected_reviewed_source_ids"] == review_case.ids
    assert report["review_diagnostics"]["response"] == review_case.clean
    assert report["review_diagnostics"]["rejection_reasons"] == []


@pytest.mark.parametrize("kind", ["missing", "unexpected", "duplicates"])
def test_bad_inventory_preserves_rejected_response_and_precise_reason(
    review_case, kind
):
    response = deepcopy(review_case.clean)
    if kind == "missing":
        response["reviewed_source_ids"].pop()
    elif kind == "unexpected":
        response["reviewed_source_ids"][0] = "invented-source"
    else:
        response["reviewed_source_ids"] = [review_case.ids[0]] * 2
    report, _ = validate(review_case, response)
    assert report["state"] == "incomplete"
    assert not report["evidence_review_complete"]
    assert report["review_findings"] == []
    assert report["review_diagnostics"]["response"] == response
    assert kind + "=" in report["review_diagnostics"]["rejection_reasons"][0]


def test_rejected_excerpt_identifies_finding_and_source(review_case):
    sid = review_case.ids[0]
    response = deepcopy(review_case.clean)
    response["findings"] = [
        {
            "category": "omission",
            "severity": "error",
            "message": "ISA scope missing.",
            "observation_ids": [],
            "source_ids": [sid],
            "evidence": [
                {"source_id": sid, "source_text": "Cover the ISA and pension."}
            ],
        }
    ]
    report, _ = validate(review_case, response)
    assert report["state"] == "incomplete"
    assert report["review_findings"] == []
    assert report["review_diagnostics"]["response"] == response
    assert report["review_diagnostics"]["rejection_reasons"] == [
        "findings[0].evidence[0]: excerpt is not verbatim in " + sid
    ]
    # A real verbatim excerpt remains accepted as a finding, not a clean pass.
    response["findings"][0]["evidence"][0]["source_text"] = "Cover the ISA."
    report, _ = validate(review_case, response)
    assert report["state"] == "failed"
    assert report["evidence_review_complete"]
    assert len(report["review_findings"]) == 1


def test_bad_json_and_schema_are_retained_for_diagnosis(review_case):
    report, _ = validate(review_case, "{broken")
    assert report["state"] == "incomplete"
    assert report["review_diagnostics"]["raw_output_text"] == "{broken"
    report, _ = validate(review_case, {"findings": []})
    assert report["state"] == "incomplete"
    assert report["review_diagnostics"]["response"] == {"findings": []}
    assert "required" in report["review_diagnostics"]["rejection_reasons"][0]


def test_dynamic_schema_does_not_change_base_schema(review_case):
    validator = ExtractionValidator()
    before = deepcopy(validator.review_schema)
    evidence = validator._load_evidence(review_case.folder, review_case.extraction)
    schema = validator._response_schema(evidence, review_case.extraction)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(review_case.clean)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(
            {"reviewed_source_ids": [], "findings": []}
        )
    assert validator.review_schema == before


def test_missing_information_can_be_noted_without_fabricating_a_quote(review_case):
    response = deepcopy(review_case.clean)
    response["findings"] = [
        {
            "category": "missing_information",
            "severity": "warning",
            "message": "A required value is not supplied. Leave it unknown and request confirmation.",
            "observation_ids": [],
            "source_ids": review_case.ids,
            "evidence": [],
        }
    ]
    report, _ = validate(review_case, response)
    assert report["evidence_review_complete"]
    assert report["review_findings"][0]["category"] == "missing_information"
    assert report["review_findings"][0]["evidence"] == []


def test_ambiguity_preserves_source_wording_instead_of_choosing_an_answer(review_case):
    response = deepcopy(review_case.clean)
    response["findings"] = [
        {
            "category": "ambiguous_information",
            "severity": "warning",
            "message": "Background scope needs clarification. Preserve the wording and ask for confirmation.",
            "observation_ids": [],
            "source_ids": [review_case.ids[1]],
            "evidence": [
                {"source_id": review_case.ids[1], "source_text": "Include background."}
            ],
        }
    ]
    report, _ = validate(review_case, response)
    assert report["evidence_review_complete"]
    assert report["review_findings"][0]["category"] == "ambiguous_information"


def test_unsupported_assertion_cannot_be_claimed_without_source_evidence(review_case):
    response = deepcopy(review_case.clean)
    response["findings"] = [
        {
            "category": "unsupported_assertion",
            "severity": "error",
            "message": "Unsupported assertion.",
            "observation_ids": [],
            "source_ids": review_case.ids,
            "evidence": [],
        }
    ]
    report, _ = validate(review_case, response)
    assert report["state"] == "incomplete"
    assert (
        "source evidence is required"
        in report["review_diagnostics"]["rejection_reasons"][0]
    )


def test_reviewer_receives_shared_reference_and_field_state_definitions(review_case):
    _, client = validate(review_case, review_case.clean)
    request = client.requests[0]
    payload = json.loads(request["messages"][1]["content"][0]["text"])
    definitions = payload["extraction_field_definitions"]
    assert set(definitions) == {"account_reference", "field_state"}
    assert "owner_text" in definitions["account_reference"]["required"]
    assert (
        "same source"
        in definitions["account_reference"]["properties"]["platform"]["description"]
    )
    assert "not_stated" in definitions["field_state"]["properties"]["state"]["enum"]
    instructions = request["messages"][0]["content"]
    assert "target_accounts[0].account_type" in instructions
    assert (
        "Annotating a field `not_stated` does not make its omission correct"
        in instructions
    )


def test_field_specific_omission_preserves_source_supported_repair_feedback(
    review_case,
):
    # Mechanical null annotations are valid, but the source actually states ISA.
    reference = {
        "account_id": None,
        "platform": None,
        "account_type": None,
        "owner_text": None,
        "reference_text": "ISA",
    }
    review_case.extraction["observations"] = [
        {
            "observation_id": "obs1",
            "source_id": review_case.ids[0],
            "observation_type": "report_scope",
            "source_text": "Cover the ISA.",
            "sections": ["introduction"],
            "evidence_scope": "client_specific",
            "data": {
                "clients": [],
                "included_accounts": [reference],
                "excluded_accounts": [],
            },
            "field_states": [
                {"field": "included_accounts[0]." + field, "state": "not_stated"}
                for field in ("account_id", "platform", "account_type", "owner_text")
            ],
        }
    ]
    response = deepcopy(review_case.clean)
    response["findings"] = [
        {
            "category": "omission",
            "severity": "error",
            "message": "Observation obs1, included_accounts[0].account_type: populate the stated ISA type; do not import an account ID.",
            "observation_ids": ["obs1"],
            "source_ids": [review_case.ids[0]],
            "evidence": [
                {"source_id": review_case.ids[0], "source_text": "Cover the ISA."}
            ],
        }
    ]
    report, _ = validate(review_case, response)
    assert report["evidence_review_complete"]
    assert report["deterministic_findings"] == []
    finding = report["review_findings"][0]
    assert "included_accounts[0].account_type" in finding["message"]
    assert finding["evidence"][0]["source_text"] == "Cover the ISA."
