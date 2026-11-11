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
    findings = check_extraction(
        extraction, evidence, ExtractionValidator().extraction_validator
    )
    assert any(
        f["category"] == "field_state" and "contradicts populated" in f["message"]
        for f in findings
    )


def test_checks_find_unsupported_excerpt():
    extraction, evidence = example()
    extraction["observations"][0]["source_text"] = "This was never in the document."
    findings = check_extraction(
        extraction, evidence, ExtractionValidator().extraction_validator
    )
    assert any(f["category"] == "quote" for f in findings)


def test_missing_live_valuation_cannot_be_covered_by_an_older_database_value():
    extraction, evidence = example()
    evidence["source-1"].update(
        role="meeting_notes",
        text="The account was showing around £18,750 during the meeting.",
    )
    findings = check_extraction(
        extraction, evidence, ExtractionValidator().extraction_validator
    )
    omitted = [f for f in findings if f["category"] == "coverage"]
    assert omitted
    assert "around £18,750" in omitted[0]["evidence"][0]["source_text"]


def test_live_valuation_coverage_accepts_qualified_amount_text_without_exact_amount():
    from agent_pipeline.validation.checks import _valuation_coverage

    extraction = {
        "observations": [
            {
                "source_id": "notes",
                "observation_type": "valuation",
                "data": {"amount": None, "amount_text": "a little over £18,750"},
            }
        ]
    }
    evidence = {
        "notes": {
            "role": "meeting_notes",
            "text": "It was showing a little over £18,750.",
        }
    }
    assert _valuation_coverage(extraction, evidence) == []
    evidence["other-source"] = evidence.pop("notes")
    assert _valuation_coverage(extraction, evidence)


def test_valuation_coverage_does_not_treat_contributions_as_account_balances():
    from agent_pipeline.validation.checks import _valuation_coverage

    evidence = {
        "notes": {
            "role": "meeting_notes",
            "text": "We agreed a £20,000 contribution from cash.",
        }
    }
    assert _valuation_coverage({"observations": []}, evidence) == []


def test_owner_field_cannot_contain_a_cross_source_rationale():
    from agent_pipeline.extraction.structured import observation

    extraction, evidence = example()
    text = "Alex's ISA is held on Platform."
    obs = observation(
        "account",
        "source-1",
        text,
        {
            "account": {
                "account_id": None,
                "platform": "Platform",
                "account_type": "ISA",
                "owner_text": "Alex (verified in the database)",
                "reference_text": "Alex's ISA",
            },
            "holders": [{"name": "Alex", "reference_text": "Alex"}],
            "status": None,
        },
        [],
    )
    obs["observation_id"] = "account"
    extraction["observations"] = [obs]
    evidence["source-1"]["text"] = text
    findings = check_extraction(
        extraction, evidence, ExtractionValidator().extraction_validator
    )
    assert any(
        f["category"] == "identity" and "literal source" in f["message"]
        for f in findings
    )
