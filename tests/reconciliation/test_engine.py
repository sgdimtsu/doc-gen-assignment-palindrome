from copy import deepcopy
import hashlib
import json

import pytest

from agent_pipeline.extraction.models import ExtractionRun, ExtractionSettings
from agent_pipeline.extraction.results import ExtractionRecords, save_run
from agent_pipeline.reconciliation.engine import reconcile
from agent_pipeline.reconcile import main
from agent_pipeline.validation.status import candidate_hash


REF = {
    "account_id": "A1",
    "platform": "Platform",
    "account_type": "ISA",
    "owner_text": "Alex",
    "reference_text": "Alex ISA",
}


def observation(kind, data, oid, source="db", text="Original source assertion."):
    def nulls(value, prefix=""):
        if value is None:
            yield prefix
        elif isinstance(value, dict):
            for k, v in value.items():
                yield from nulls(v, prefix + "." + k if prefix else k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from nulls(v, f"{prefix}[{i}]")

    return {
        "observation_id": oid,
        "source_id": source,
        "source_text": text,
        "observation_type": kind,
        "data": deepcopy(data),
        "evidence_scope": "client_specific",
        "sections": ["background_objectives"],
        "field_states": [{"field": p, "state": "not_stated"} for p in nulls(data)],
    }


def valuation(
    oid="v1", amount="100.10", when="2026-01-01", precision="exact", currency="GBP"
):
    return observation(
        "valuation",
        {
            "account": REF,
            "amount": amount,
            "amount_text": amount or "around £120",
            "currency": currency,
            "valuation_date": when,
            "date_text": when,
            "precision": precision,
        },
        oid,
    )


def action(oid="act", status="agreed", amount=None):
    return observation(
        "action",
        {
            "action_type": "sell",
            "source_accounts": [REF],
            "target_accounts": [],
            "amount": amount,
            "amount_text": amount,
            "currency": "GBP",
            "precision": "exact" if amount else "unspecified",
            "amount_basis": "specified" if amount else "full_balance",
            "agreement_status": status,
            "allocation_text": None,
            "rationale_text": None,
            "conditions": [],
            "timing_text": None,
        },
        oid,
        "notes",
    )


@pytest.fixture
def evidence():
    sources = [
        {
            "source_id": sid,
            "role": role,
            "filename": filename,
            "format": fmt,
            "read_status": "read",
            "document_date": None,
        }
        for sid, role, filename, fmt in [
            ("request", "report_request", "report_request.docx", "docx"),
            ("db", "account_records", "client_data_db.json", "json"),
            ("notes", "meeting_notes", "meeting_notes.docx", "docx"),
            ("spec", "report_specification", "template_spec.md", "markdown"),
        ]
    ]
    account = observation(
        "account",
        {
            "account": REF,
            "holders": [{"name": "Alex", "reference_text": "Alex"}],
            "status": "open",
        },
        "account",
    )
    scope = observation(
        "report_scope",
        {
            "clients": [{"name": "Alex", "reference_text": "Alex"}],
            "included_accounts": [REF],
            "excluded_accounts": [],
        },
        "scope",
        "request",
    )
    return {
        "sources": sources,
        "observations": [account, scope, valuation()],
        "extraction_issues": [],
    }


def test_resolves_report_facts_without_mutating_extraction(evidence):
    before = deepcopy(evidence)
    result = reconcile(evidence)
    assert "ready_for_writing" not in result
    assert result["scope"]["included_account_keys"] == ["account-0001"]
    assert result["accounts"][0]["holders"] == ["Alex"]
    assert result["accounts"][0]["valuation"]["observation_ids"] == ["v1"]
    assert result["accounts"][0]["valuation"]["data"]["amount"] == "100.10"
    assert evidence == before


def test_latest_approximate_valuation_remains_qualified(evidence):
    evidence["observations"].append(valuation("v2", None, "2026-02-01", "approximate"))
    result = reconcile(evidence)
    selected = result["accounts"][0]["valuation"]
    assert selected["observation_ids"] == ["v2"]
    assert selected["data"]["amount"] is None
    assert selected["data"]["amount_text"] == "around £120"


@pytest.mark.parametrize(
    "failure",
    ["same_date_conflict", "undated", "missing_latest", "currency"],
)
def test_unresolvable_values_are_not_silently_selected(evidence, failure):
    other = valuation("v2", "200", "2026-02-01")
    if failure == "same_date_conflict":
        other["data"]["valuation_date"] = "2026-01-01"
    elif failure == "undated":
        other["data"]["valuation_date"] = None
    elif failure == "missing_latest":
        other["data"]["amount"] = None
    elif failure == "currency":
        other["data"]["currency"] = "USD"
    evidence["observations"].append(other)
    result = reconcile(evidence)
    assert result["accounts"][0]["valuation"] is None
    assert any(i["issue_type"] == "valuation_unresolved" for i in result["issues"])


def test_equal_latest_values_keep_both_sources_as_support(evidence):
    other = valuation("v2", "100.100")
    other["source_id"] = "notes"
    evidence["observations"].append(other)
    selected = reconcile(evidence)["accounts"][0]["valuation"]
    assert selected["observation_ids"] == ["v1", "v2"]


def test_zero_is_a_supported_value(evidence):
    evidence["observations"][-1] = valuation(amount="0")
    assert reconcile(evidence)["accounts"][0]["valuation"]["data"]["amount"] == "0"


def test_conflicting_equal_authority_scope_blocks(evidence):
    extra_source = {**evidence["sources"][0], "source_id": "request2"}
    evidence["sources"].append(extra_source)
    scope = deepcopy(evidence["observations"][1])
    scope.update(observation_id="scope2", source_id="request2")
    scope["data"]["included_accounts"] = []
    scope["data"]["excluded_accounts"] = [REF]
    evidence["observations"].append(scope)
    result = reconcile(evidence)
    assert result["requires_review"]
    assert result["scope"]["included_account_keys"] == []


def test_request_scope_wins_but_secondary_scope_remains_visible(evidence):
    scope = deepcopy(evidence["observations"][1])
    scope.update(observation_id="scope2", source_id="notes")
    scope["data"]["included_accounts"] = []
    evidence["observations"].append(scope)
    result = reconcile(evidence)
    assert result["scope"]["observation_ids"] == ["scope"]
    assert "scope_evidence" not in result
    assert any("scope2" in i["observation_ids"] for i in result["issues"])
    assert any(i["issue_type"] == "secondary_evidence" for i in result["issues"])


def test_account_records_take_precedence_for_identity(evidence):
    other = deepcopy(evidence["observations"][0])
    other.update(observation_id="other", source_id="notes")
    other["data"]["account"]["platform"] = "Other platform"
    evidence["observations"].append(other)
    account = reconcile(evidence)["accounts"][0]
    assert account["platform"] == "Platform"
    assert account["observation_ids"] == ["account"]


def test_conflicting_authoritative_identity_blocks(evidence):
    other = deepcopy(evidence["observations"][0])
    other["observation_id"] = "other"
    other["data"]["account"]["platform"] = "Other platform"
    evidence["observations"].append(other)
    assert reconcile(evidence)["requires_review"]


@pytest.mark.parametrize("status", ["proposed", "discussed", "deferred", "rejected"])
def test_unagreed_actions_are_not_promoted_to_recommendations(evidence, status):
    evidence["observations"].append(action(status=status))
    assert reconcile(evidence)["actions"][0]["data"]["agreement_status"] == status


def test_conflicting_agreed_actions_block_and_are_not_combined(evidence):
    evidence["observations"] += [action("a1", amount="10"), action("a2", amount="20")]
    result = reconcile(evidence)
    assert result["requires_review"]
    assert len(result["actions"]) == 2
    assert any(i["issue_type"] == "action_conflict" for i in result["issues"])


def test_meeting_actions_preferred_to_request_with_secondary_evidence_kept(evidence):
    other = action("other", amount="20")
    other["source_id"] = "request"
    evidence["observations"] += [other, action()]
    result = reconcile(evidence)
    assert result["actions"][0]["observation_ids"] == ["act"]
    assert "action_evidence" not in result
    assert any("other" in i["observation_ids"] for i in result["issues"])


def test_conditional_funding_and_unconfirmed_charges_require_review(evidence):
    funding = observation(
        "funding",
        {
            "people": [],
            "origin_text": "Future receipt",
            "account": None,
            "amount": "100",
            "amount_text": "100",
            "currency": "GBP",
            "precision": "exact",
            "receipt_status": "expected",
            "receipt_date": None,
            "timing_text": None,
            "commitments": [],
            "conditions": ["Subject to completion"],
        },
        "fund",
        "notes",
    )
    charge = observation(
        "charge",
        {
            "charge_type": "platform",
            "account": None,
            "platform": "Platform",
            "rate_percent": "0.5",
            "amount": None,
            "currency": None,
            "basis_text": None,
            "frequency_text": None,
            "confirmation_status": "requires_confirmation",
        },
        "charge",
        "notes",
    )
    evidence["observations"] += [funding, charge]
    result = reconcile(evidence)
    assert result["funding"][0]["data"]["conditions"] == ["Subject to completion"]
    assert any(i["issue_type"] == "confirmation_required" for i in result["issues"])
    assert result["funding"][0]["data"]["receipt_status"] == "expected"


def test_general_illustrative_holdings_are_excluded(evidence):
    other = deepcopy(evidence["observations"][0])
    other.update(observation_id="illustration", evidence_scope="general_illustrative")
    other["data"]["account"]["account_id"] = "DEMO"
    evidence["observations"].append(other)
    assert len(reconcile(evidence)["accounts"]) == 1


def test_scope_reference_without_id_uses_unique_platform_type(evidence):
    evidence["observations"][1]["data"]["included_accounts"][0]["account_id"] = None
    result = reconcile(evidence)
    assert result["requires_review"]
    assert result["scope"]["included_account_keys"] == ["account-0001"]
    assert not any(
        i["issue_type"] == "unresolved_account_match" for i in result["issues"]
    )


def test_cli_connects_approved_run_to_reconciled_output(evidence, tmp_path, capsys):
    raw = deepcopy(evidence)
    inventory = [
        {k: s[k] for k in ("source_id", "filename", "format", "read_status")}
        for s in raw["sources"]
    ]
    for source in raw["sources"]:
        for field in ("filename", "format", "read_status"):
            del source[field]
    for item in raw["observations"]:
        del item["observation_id"]
    run = ExtractionRun(state="complete", inventory=inventory, response=raw)
    assembled = ExtractionRecords().assemble(run.response, run.inventory, run.run_id)
    run.validation_reports = [
        {
            "state": "passed",
            "evidence_review_complete": True,
            "errors": [],
            "deterministic_findings": [],
            "review_findings": [],
            "candidate_sha256": candidate_hash(raw),
            "extraction_sha256": hashlib.sha256(
                json.dumps(assembled, sort_keys=True).encode()
            ).hexdigest(),
        }
    ]
    folder = save_run(
        tmp_path, "example", run, ExtractionSettings(model="offline-test")
    )
    assert main(["--run-dir", str(folder)]) == 0
    result = json.loads((folder / "reconciliation.json").read_text())
    assert "ready_for_writing" not in result
    assert "Reconciliation: draft" in capsys.readouterr().out


def test_cli_rejects_partial_run_without_output(tmp_path):
    run = ExtractionRun(errors=["Extraction failed"])
    folder = save_run(
        tmp_path, "example", run, ExtractionSettings(model="offline-test")
    )
    with pytest.raises(SystemExit) as error:
        main(["--run-dir", str(folder)])
    assert error.value.code == 1
    assert not (folder / "reconciliation.json").exists()


def test_scoped_proposed_account_is_not_created_as_an_existing_holding(evidence):
    evidence["observations"][0]["data"]["status"] = "proposed"
    result = reconcile(evidence)
    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["proposed"]
    assert not any(
        i["issue_type"] == "proposed_existing_id_conflict" for i in result["issues"]
    )


def test_closed_account_in_scope_blocks_writing(evidence):
    evidence["observations"][0]["data"]["status"] = "closed"
    result = reconcile(evidence)
    assert result["requires_review"]
    assert any(i["issue_type"] == "closed_account_in_scope" for i in result["issues"])


def test_conflicting_fees_are_preserved_and_flagged(evidence):
    data = {
        "charge_type": "platform",
        "account": None,
        "platform": "Platform",
        "rate_percent": "0.5",
        "amount": None,
        "currency": "GBP",
        "basis_text": "Portfolio value",
        "frequency_text": "annual",
        "confirmation_status": "explicitly_confirmed",
    }
    evidence["observations"].append(observation("charge", data, "c1", "notes"))
    evidence["observations"].append(
        observation("charge", {**data, "rate_percent": "1.0"}, "c2", "request")
    )
    result = reconcile(evidence)
    assert len(result["charges"]) == 2
    assert any(i["issue_type"] == "charge_conflict" for i in result["issues"])


def test_output_keeps_only_needed_fact_views_and_small_provenance(evidence):
    evidence["observations"].append(action())
    result = reconcile(evidence)
    assert (
        not {"calculations", "section_facts", "scope_evidence", "action_evidence"}
        & result.keys()
    )
    selected = result["accounts"][0]["valuation"]
    assert set(selected) == {
        "data",
        "source_text",
        "field_states",
        "sections",
        "observation_ids",
    }
    assert selected["source_text"] == evidence["observations"][2]["source_text"]
    assert selected["observation_ids"] == ["v1"]
    assert "references" not in result["accounts"][0]
    assert "identity_rule" not in result["accounts"][0]


def test_valuation_uncertainty_is_handled_by_explicit_fields(evidence):
    value = evidence["observations"][2]
    value["field_states"].append({"field": "valuation_date", "state": "ambiguous"})
    result = reconcile(evidence)
    assert result["accounts"][0]["valuation"] is None
    assert any(i["issue_type"] == "valuation_unresolved" for i in result["issues"])


def test_advisory_validation_findings_become_report_review_notes(evidence):
    report = {
        "state": "failed",
        "evidence_review_complete": True,
        "errors": [],
        "extraction_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True).encode()
        ).hexdigest(),
        "deterministic_findings": [],
        "review_findings": [
            {
                "category": "omission",
                "severity": "error",
                "message": "Capture a missed source fact.",
                "observation_ids": [],
            },
            {
                "category": "missing_information",
                "severity": "warning",
                "message": "Charges need confirmation.",
                "observation_ids": [],
            },
            {
                "category": "ambiguous_information",
                "severity": "warning",
                "message": "Preserve uncertain wording.",
                "observation_ids": [],
            },
        ],
    }
    result = reconcile(evidence, [report])
    assert result["validation_state"] == "failed"
    assert result["requires_review"]
    assert {i["issue_type"] for i in result["issues"]} >= {
        "validation_omission",
        "validation_missing_information",
        "validation_ambiguous_information",
    }
    assert "ready_for_writing" not in result
    assert any("Validator concern" in i["message"] for i in result["issues"])


def test_conflict_is_handled_by_reconciliation_without_stopping_draft(evidence):
    evidence["observations"].append(valuation("other", "200", "2026-01-01"))
    result = reconcile(evidence)
    assert result["accounts"][0]["valuation"] is None
    assert any(i["issue_type"] == "valuation_unresolved" for i in result["issues"])
    assert result["requires_review"]
    assert "ready_for_writing" not in result


def test_stale_findings_are_one_notice_not_repeated_as_current_errors(evidence):
    findings = [
        {
            "category": "field_state",
            "message": "Old missing field.",
            "severity": "error",
            "observation_ids": ["v1"],
        }
    ]
    report = {
        "state": "failed",
        "evidence_review_complete": True,
        "extraction_sha256": "old-candidate",
        "errors": [],
        "deterministic_findings": findings,
        "review_findings": findings,
    }
    result = reconcile(evidence, [report])
    assert (
        len([i for i in result["issues"] if i["issue_type"].startswith("validation_")])
        == 1
    )
    assert result["validation_state"] == "stale"
    assert not any(
        i["issue_type"] == "validation_field_state" for i in result["issues"]
    )
    assert report["deterministic_findings"] == findings


def test_current_code_and_model_duplicates_are_reported_once(evidence):
    finding = {
        "category": "field_state",
        "message": "Null field lacks an explicit state: account Next step: Use relative paths.",
        "severity": "error",
        "observation_ids": ["v1"],
    }
    duplicate = {
        **finding,
        "message": "Null field lacks an explicit state: account. Next step: Use relative paths.",
    }
    report = {
        "state": "failed",
        "evidence_review_complete": True,
        "errors": [],
        "extraction_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True).encode()
        ).hexdigest(),
        "deterministic_findings": [finding],
        "review_findings": [duplicate],
    }
    result = reconcile(evidence, [report])
    assert (
        len(
            [i for i in result["issues"] if i["issue_type"] == "validation_field_state"]
        )
        == 1
    )


def test_general_fee_requirement_is_a_review_gap_not_a_confirmed_client_charge(
    evidence,
):
    data = {
        "charge_type": "platform",
        "account": None,
        "platform": None,
        "rate_percent": None,
        "amount": None,
        "currency": None,
        "basis_text": "Platform charge required",
        "frequency_text": None,
        "confirmation_status": "not_stated",
    }
    requirement = observation("charge", data, "requirement", "spec")
    requirement.update(evidence_scope="general_illustrative", sections=["fees_charges"])
    evidence["observations"].append(requirement)
    result = reconcile(evidence)
    assert result["charges"] == []
    assert any(
        i["issue_type"] == "confirmation_required" and "platform" in i["message"]
        for i in result["issues"]
    )


def test_joint_owner_reference_links_newer_approximate_gia_valuation(evidence):
    joint_ref = {
        **REF,
        "account_id": "J1",
        "account_type": "General Investment Account",
        "owner_text": "joint",
    }
    for index, owner in enumerate(("Alex Taylor", "Sam Jordan")):
        record = observation(
            "account",
            {
                "account": joint_ref,
                "holders": [{"name": owner, "reference_text": owner}],
                "status": "open",
            },
            f"joint-{index}",
        )
        evidence["observations"].append(record)
    old = valuation("joint-old", "30000", "2026-03-01")
    old["data"]["account"] = joint_ref
    newer = valuation("joint-live", None, "2026-05-01", "approximate")
    newer["data"]["amount_text"] = "around £38,000"
    newer["data"]["account"] = {**joint_ref, "account_id": None, "account_type": "GIA"}
    newer["field_states"] = [
        {"field": "account.account_id", "state": "not_stated"},
        {"field": "amount", "state": "qualified_value"},
    ]
    evidence["observations"] += [old, newer]
    result = reconcile(evidence)
    joint = next(a for a in result["accounts"] if a["account_id"] == "J1")
    assert joint["valuation"]["observation_ids"] == ["joint-live"]
    assert joint["valuation"]["data"]["amount"] is None
    assert joint["valuation"]["data"]["amount_text"] == "around £38,000"


def test_retain_no_sale_statement_does_not_require_a_transaction_amount(evidence):
    retained = action()
    retained["data"].update(
        action_type="retain", amount=None, amount_text=None, amount_basis="unspecified"
    )
    retained["field_states"] = [
        {"field": "amount", "state": "not_applicable"},
        {"field": "amount_text", "state": "not_applicable"},
    ]
    evidence["observations"].append(retained)
    result = reconcile(evidence)
    assert not any(i["issue_type"] == "amount_unconfirmed" for i in result["issues"])
    assert result["actions"][0]["data"]["agreement_status"] == "agreed"


def test_unspecified_contribution_still_requires_amount_confirmation(evidence):
    contribution = action()
    contribution["data"].update(
        action_type="contribute", amount_basis="partial_unspecified"
    )
    evidence["observations"].append(contribution)
    result = reconcile(evidence)
    assert any(i["issue_type"] == "amount_unconfirmed" for i in result["issues"])


def test_final_code_checks_survive_stale_llm_review(evidence):
    report = {
        "state": "passed",
        "extraction_sha256": "old-candidate",
        "review_findings": [{"category": "omission", "message": "Outdated finding."}],
        "final_checks": {
            "findings": [
                {
                    "category": "quote",
                    "message": "Final evidence is unsupported.",
                    "observation_ids": [],
                    "severity": "error",
                }
            ],
            "errors": [],
        },
    }
    reconciled = reconcile(evidence, [report])
    assert reconciled["validation_state"] == "stale"
    assert any(
        "Final evidence is unsupported" in issue["message"]
        for issue in reconciled["issues"]
    )
    assert not any(
        "Outdated finding" in issue["message"] for issue in reconciled["issues"]
    )


def test_contribution_assertion_cannot_replace_known_account_value(evidence):
    mistaken = valuation("contribution-misclassified", amount="20000", when=None)
    mistaken["source_text"] = "We agreed to move £20,000 from cash into the ISA."
    evidence["observations"].append(mistaken)
    result = reconcile(evidence)
    account = next(a for a in result["accounts"] if a["account_id"] == "A1")
    assert account["valuation"]["data"]["amount"] != "20000"
    assert any(i["issue_type"] == "valuation_unresolved" for i in result["issues"])


def test_partially_matched_scope_remains_explicit_and_incomplete(evidence):
    scope = next(
        o for o in evidence["observations"] if o["observation_type"] == "report_scope"
    )
    scope["data"]["included_accounts"].append(
        {
            **REF,
            "account_id": None,
            "account_type": "SIPP",
            "owner_text": "Unknown",
            "reference_text": "Proposed SIPP",
        }
    )
    result = reconcile(evidence)
    assert result["scope"]["included_account_keys"]
    assert result["scope"]["complete"] is False
    assert (
        result["scope"]["unresolved_requests"][0]["reference"]["reference_text"]
        == "Proposed SIPP"
    )
    assert any(i["issue_type"] == "scope_incomplete" for i in result["issues"])
    from agent_pipeline.reconciliation.report import build_report

    plan = build_report(
        result,
        "## Introduction\nState which of the client's accounts the report covers.",
    )
    text = "\n".join(b["text"] for b in plan["blocks"].values())
    assert "Confirmed accounts within the requested scope" in text
    assert "Proposed SIPP" in text
    assert "This draft covers:" not in text


def test_new_account_request_is_not_lost_when_extractor_omits_it(evidence):
    evidence["request_fields"] = [
        {
            "source_id": "request",
            "label": "Accounts covered",
            "value": "Platform ISA and a new joint account",
        }
    ]
    result = reconcile(evidence)
    assert result["scope"]["complete"] is False
    assert any(
        "new joint account" in p["reference"]["reference_text"]
        for p in result["scope"]["unresolved_requests"]
    )
