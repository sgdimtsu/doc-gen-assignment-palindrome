"""POC rules that turn extracted assertions into traceable report facts."""

from collections import defaultdict
from copy import deepcopy
import json
import re

from agent_pipeline.reconciliation.accounts import match_accounts
from agent_pipeline.reconciliation.valuations import select_valuation
from agent_pipeline.validation.status import validation_state


def _fact(observations):
    first = observations[0]
    return {
        "data": deepcopy(first["data"]),
        "observation_ids": [o["observation_id"] for o in observations],
        "source_text": first["source_text"],
        "sections": sorted(
            {section for o in observations for section in o["sections"]}
        ),
        "field_states": deepcopy(first["field_states"]),
    }


def reconcile(extraction: dict, validation_reports=None, run_errors=()) -> dict:
    # Structural validity is required; validation concerns become draft review notes.
    observations = [
        o
        for o in extraction["observations"]
        if o["evidence_scope"] != "general_illustrative"
        or o["observation_type"] == "instruction"
    ]
    roles = {s["source_id"]: s["role"] for s in extraction["sources"]}
    by_id = {o["observation_id"]: o for o in observations}
    grouped = defaultdict(list)
    for observation in observations:
        grouped[observation["observation_type"]].append(observation)
    matches = match_accounts(
        {
            "observations": observations,
            "extraction_issues": extraction["extraction_issues"],
        }
    )
    issues = []

    def issue(kind, message, ids=(), keys=(), critical=False, field=None):
        issues.append(
            {
                "issue_type": kind,
                "message": message,
                "severity": "error" if critical else "warning",
                "observation_ids": list(ids),
                "account_keys": list(keys),
                "field": field,
            }
        )

    links = {}
    for account in matches["accounts"]:
        for ref in account["references"]:
            links[(ref["observation_id"], ref["field"])] = account["account_key"]
    for problem in matches["issues"]:
        oid = problem.get("observation_id")
        issue(
            problem["issue_type"],
            problem.get("reason", "Proposed account reuses an existing account ID"),
            [oid] if oid else [],
            problem.get("candidate_account_keys", problem.get("account_keys", [])),
            critical=True,
            field=problem.get("field"),
        )

    def prefer(items, role):
        preferred = [o for o in items if roles[o["source_id"]] == role]
        return preferred or items

    accounts = []
    for account in matches["accounts"]:
        result = {
            key: account[key] for key in ("account_key", "account_id", "proposed")
        }
        records = [
            by_id[oid]
            for oid in account["observation_ids"]
            if by_id[oid]["observation_type"] == "account"
        ]
        authoritative = prefer(records, "account_records")
        result["observation_ids"] = [o["observation_id"] for o in authoritative]
        result["holders"] = sorted(
            {
                p["name"]
                for o in authoritative
                for p in o["data"]["holders"]
                if p["name"]
            }
        )
        for field in ("platform", "account_type", "status"):
            values = {
                o["data"]["status"]
                if field == "status"
                else o["data"]["account"][field]
                for o in authoritative
            }
            values.discard(None)
            result[field] = next(iter(values)) if len(values) == 1 else None
            if len(values) > 1:
                issue(
                    "identity_conflict",
                    "Authoritative account records disagree on " + field,
                    result["observation_ids"],
                    [account["account_key"]],
                    True,
                    field,
                )
        valuations = [
            by_id[r["observation_id"]]
            for r in account["references"]
            if r["field"] == "account"
            and by_id[r["observation_id"]]["observation_type"] == "valuation"
        ]
        selected, problem = select_valuation(valuations)
        result["valuation"] = _fact(selected) if selected else None
        if problem and not account["proposed"]:
            issue(
                "valuation_unresolved",
                problem,
                [o["observation_id"] for o in valuations],
                [account["account_key"]],
                field="valuation",
            )
        accounts.append(result)

    scopes = prefer(grouped["report_scope"], "report_request")
    scope = {
        "included_account_keys": [],
        "excluded_account_keys": [],
        "clients": [],
        "observation_ids": [o["observation_id"] for o in scopes],
    }
    source_scopes = defaultdict(lambda: {"included": set(), "excluded": set()})
    for observation in scopes:
        oid = observation["observation_id"]
        source_scopes[
            observation["source_id"]
        ]  # Include explicit empty-scope assertions.
        scope["clients"].extend(
            p["name"] for p in observation["data"]["clients"] if p["name"]
        )
        for category in ("included", "excluded"):
            for i, _ in enumerate(observation["data"][category + "_accounts"]):
                key = links.get((oid, f"{category}_accounts[{i}]"))
                if key:
                    source_scopes[observation["source_id"]][category].add(key)
    for category in ("included", "excluded"):
        scope[category + "_account_keys"] = sorted(
            {key for item in source_scopes.values() for key in item[category]}
        )
    scope["clients"] = sorted(set(scope["clients"]))
    signatures = {
        (tuple(sorted(s["included"])), tuple(sorted(s["excluded"])))
        for s in source_scopes.values()
    }
    overlap = set(scope["included_account_keys"]) & set(scope["excluded_account_keys"])
    if len(signatures) > 1 or overlap:
        issue(
            "scope_conflict",
            "Equally authoritative scope instructions disagree",
            scope["observation_ids"],
            overlap,
            True,
        )
        scope["included_account_keys"] = []
    if not scopes or not scope["included_account_keys"]:
        issue(
            "scope_missing",
            "A supported non-empty report scope could not be established",
            scope["observation_ids"],
            critical=True,
        )

    # Each statement remains separate unless this stage has an explicit resolution
    # rule. Never turn a discussion into agreement or net conditional funding.
    output = {
        "scope": scope,
        "accounts": accounts,
        "actions": [],
        "funding": [],
        "objectives": [],
        "charges": [],
        "tax_statements": [],
        "instructions": [],
        "unclassified": [],
    }
    actions = prefer(grouped["action"], "meeting_notes")
    for kind, selected in (("report_scope", scopes), ("action", actions)):
        selected_ids = {o["observation_id"] for o in selected}
        secondary = [
            o for o in grouped[kind] if o["observation_id"] not in selected_ids
        ]
        if secondary:
            issue(
                "secondary_evidence",
                "Other "
                + kind
                + " assertions are retained for review; the fact-type authority rule selected the primary source",
                [o["observation_id"] for o in secondary],
            )
    for observation in actions:
        fact = _fact([observation])
        for direction in ("source", "target"):
            fact[direction + "_account_keys"] = sorted(
                {
                    key
                    for (oid, field), key in links.items()
                    if oid == observation["observation_id"]
                    and field.startswith(direction + "_accounts[")
                }
            )
        account_keys = fact["source_account_keys"] + fact["target_account_keys"]
        agreed = observation["data"]["agreement_status"] == "agreed"
        outside = set(fact["target_account_keys"]) - set(scope["included_account_keys"])
        if agreed and outside:
            issue(
                "action_outside_scope",
                "An agreed target is outside the report scope",
                fact["observation_ids"],
                sorted(outside),
                True,
            )
        if observation["data"]["agreement_status"] == "unclear":
            issue(
                "action_unclear",
                "Action agreement requires confirmation",
                fact["observation_ids"],
                account_keys,
                True,
            )
        if (
            agreed
            and observation["data"]["action_type"] != "retain"
            and observation["data"]["amount"] is None
            and observation["data"]["amount_basis"] != "full_balance"
        ):
            issue(
                "amount_unconfirmed",
                "Do not state an exact transaction amount; retain wording or request confirmation",
                fact["observation_ids"],
                account_keys,
                field="amount",
            )
        output["actions"].append(fact)
    # Flag incompatible assertions concerning the same explicit action and targets.
    action_groups = defaultdict(list)
    for fact in output["actions"]:
        action_groups[
            (
                fact["data"]["action_type"],
                tuple(fact["source_account_keys"]),
                tuple(fact["target_account_keys"]),
            )
        ].append(fact)
    for facts in action_groups.values():
        signatures = {
            json.dumps(
                {
                    k: f["data"][k]
                    for k in (
                        "agreement_status",
                        "amount",
                        "amount_text",
                        "currency",
                        "amount_basis",
                        "conditions",
                    )
                },
                sort_keys=True,
            )
            for f in facts
        }
        if len(facts) > 1 and len(signatures) > 1:
            issue(
                "action_conflict",
                "Actions for the same matched accounts disagree; no combined instruction was inferred",
                [oid for f in facts for oid in f["observation_ids"]],
                facts[0]["source_account_keys"] + facts[0]["target_account_keys"],
                True,
            )

    for observation_type, target in (
        ("funding", "funding"),
        ("objective_circumstance", "objectives"),
        ("charge", "charges"),
        ("tax_statement", "tax_statements"),
        ("instruction", "instructions"),
        ("unclassified", "unclassified"),
    ):
        for observation in grouped[observation_type]:
            fact = _fact([observation])
            output[target].append(fact)
            data = observation["data"]
            if (
                observation_type in {"charge", "tax_statement"}
                and data["confirmation_status"] != "explicitly_confirmed"
            ):
                issue(
                    "confirmation_required",
                    "This " + observation_type + " requires human confirmation",
                    fact["observation_ids"],
                )
            if observation_type == "unclassified" or (
                observation_type == "instruction"
                and data["instruction_type"] == "source_authority"
            ):
                issue(
                    "interpretation_required",
                    "Source guidance or unclassified evidence needs review; fixed rules do not interpret free-text policy",
                    fact["observation_ids"],
                )
    charge_groups = defaultdict(list)
    for fact in output["charges"]:
        data = fact["data"]
        reference = data["account"] or {}
        key = (
            data["charge_type"],
            reference.get("account_id"),
            data["platform"],
            data["basis_text"],
            data["frequency_text"],
        )
        charge_groups[key].append(fact)
    for facts in charge_groups.values():
        stated = {
            (f["data"]["rate_percent"], f["data"]["amount"], f["data"]["currency"])
            for f in facts
        }
        if len(stated) > 1:
            issue(
                "charge_conflict",
                "Fee assertions disagree; retain alternatives for human confirmation rather than choosing a rate",
                [oid for f in facts for oid in f["observation_ids"]],
            )
    for problem in extraction["extraction_issues"]:
        issue(
            "extraction_" + problem["issue_type"],
            problem["description"],
            problem["observation_ids"],
        )
    for account in accounts:
        account["in_report_scope"] = (
            account["account_key"] in scope["included_account_keys"]
        )
    included = [
        a
        for a in accounts
        if a["account_key"] in scope["included_account_keys"] and not a["proposed"]
    ]
    for account in included:
        if account["status"] == "closed":
            issue(
                "closed_account_in_scope",
                "A covered account is recorded as closed; confirm the requested action",
                account["observation_ids"],
                [account["account_key"]],
                True,
            )
        if (
            not account["holders"]
            or not account["platform"]
            or not account["account_type"]
        ):
            issue(
                "identity_missing",
                "Account ownership, platform or type is not established",
                account["observation_ids"],
                [account["account_key"]],
                True,
            )
    output["issues"] = issues
    reports = validation_reports or []
    state = validation_state(None, reports, extraction)
    output["validation_state"] = state
    if state != "passed":
        message = (
            "The final candidate changed after validation. Earlier findings may be outdated; see validation.json and verify this draft."
            if state == "stale"
            else "Validation is " + state + "; this draft requires human verification"
        )
        issue("validation_" + state, message)
    if reports and state != "stale":
        report = reports[-1]
        seen = set()
        for finding in report.get("deterministic_findings", []) + report.get(
            "review_findings", []
        ):
            ids = finding.get("observation_ids", [])
            key = (
                finding["category"],
                tuple(sorted(ids)),
                re.sub(r"\W+", " ", finding["message"]).strip().casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            issue(
                "validation_" + finding["category"],
                "Validator concern — verify: " + finding["message"],
                ids,
                critical=finding.get("severity") == "error",
            )
        for error in report.get("errors", []):
            issue("validation_incomplete", error)
    # A template-derived fee requirement is not a client fee, but its absence
    # still needs a review note when illustrative observations were filtered out.
    for observation in extraction["observations"]:
        if (
            observation["observation_id"] not in by_id
            and observation["observation_type"] == "charge"
            and "fees_charges" in observation["sections"]
            and observation["data"]["rate_percent"] is None
            and observation["data"]["amount"] is None
        ):
            charge_type = observation["data"]["charge_type"]
            supplied = any(
                f["data"]["charge_type"] == charge_type for f in output["charges"]
            )
            if not supplied:
                issue(
                    "confirmation_required",
                    "Confirm the "
                    + charge_type.replace("_", " ")
                    + " charge; no client-specific figure was supplied",
                    [observation["observation_id"]],
                )
    for error in run_errors:
        issue("extraction_incomplete", error)
    for source in extraction["sources"]:
        if source["read_status"] != "read":
            issue(
                "source_unread",
                "Evidence from " + source["filename"] + " was not fully read",
            )
    output["requires_review"] = bool(issues)
    return output
