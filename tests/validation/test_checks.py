from copy import deepcopy

from agent_pipeline.validation.checks import check_extraction
from agent_pipeline.validation.review import ExtractionValidator


def example():
    extraction = {
        "sources": [
            {
                "source_id": "source-1",
                "filename": "notes.md",
                "format": "markdown",
                "read_status": "read",
                "role": "meeting_notes",
                "document_date": None,
            }
        ],
        "observations": [
            {
                "observation_id": "obs-1",
                "observation_type": "unclassified",
                "source_id": "source-1",
                "source_text": "A relevant detail.",
                "sections": [],
                "evidence_scope": "client_specific",
                "data": {"reason_unclassified": "uncertain_relevance"},
                "field_states": [],
            }
        ],
        "extraction_issues": [],
    }
    evidence = {"source-1": {"text": "A relevant detail."}}
    return extraction, evidence


def test_checks_accept_supported_document_evidence_without_mutating_input():
    extraction, evidence = example()
    before = deepcopy(extraction)
    validator = ExtractionValidator()
    assert check_extraction(extraction, evidence, validator.extraction_validator) == []
    assert extraction == before


def test_checks_find_missing_state_on_populated_field():
    extraction, evidence = example()
    extraction["observations"][0]["field_states"] = [
        {"field": "reason_unclassified", "state": "not_stated"}
    ]
    findings = ExtractionValidator().deterministic_checks(extraction, evidence)
    assert any(
        f["category"] == "field_state" and "contradicts populated" in f["message"]
        for f in findings
    )


def test_checks_find_unsupported_excerpt():
    extraction, evidence = example()
    extraction["observations"][0]["source_text"] = "This was never in the document."
    findings = ExtractionValidator().deterministic_checks(extraction, evidence)
    assert any(f["category"] == "quote" for f in findings)
