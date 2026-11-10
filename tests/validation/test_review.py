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

    def create(self, **kwargs):
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
    report = ExtractionValidator().validate(case.extraction, case.folder, client)
    return report, client


def test_rules_only_review_cannot_claim_evidence_reviewed_pass(review_case):
    before = deepcopy(review_case.extraction)
    report = ExtractionValidator().validate(review_case.extraction, review_case.folder)
    assert report["state"] == "not_reviewed"
    assert report["evidence_review_complete"] is False
    assert report["errors"] == []
    assert len(report["source_hashes"]) == 2
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
