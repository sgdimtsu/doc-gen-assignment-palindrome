"""Build the report's factual blocks and section decisions before the writer runs."""

import re
from decimal import Decimal, InvalidOperation

FCA_LINE = "This firm is authorised and regulated by the Financial Conduct Authority."


def specification_sections(specification):
    headings = list(re.finditer(r"^##[ \t]+(.+)$", specification, re.MULTILINE))
    sections = []
    for index, heading in enumerate(headings):
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(specification)
        )
        sections.append(
            dict(
                title=heading[1].strip(),
                requirements=specification[heading.end() : end].strip(),
            )
        )
    if not sections or len({s["title"] for s in sections}) != len(sections):
        raise ValueError(
            "Client specification requires distinct level-two section headings"
        )
    return sections


def fixed_wording(requirements):
    fixed = [
        " ".join(m[1].split())
        for m in re.finditer(r'word for word:\s*"([^\"]+)"', requirements, re.I)
    ]
    if re.search(r"FCA.*authori[sz]ation|authori[sz]ation.*FCA", requirements, re.I):
        if not any("Financial Conduct Authority" in text for text in fixed):
            fixed.append(FCA_LINE)
    return fixed


def money(data):
    if data.get("precision") not in {None, "exact"}:
        return data.get("amount_text") or "Value requires confirmation"
    amount = data.get("amount")
    if amount is None:
        return "Not supplied"
    try:
        number = Decimal(amount)
        if not number.is_finite():
            raise InvalidOperation
        symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(
            data.get("currency"), (data.get("currency") or "") + " "
        )
        return symbol + format(number, ",f")
    except (InvalidOperation, ValueError, TypeError):
        return "Value requires confirmation"


def review_note(issue):
    kind = issue["issue_type"]
    messages = {
        "unresolved_account_match": "An account reference could not be matched uniquely. Confirm its identity and intended use.",
        "scope_incomplete": "Only part of the requested scope could be matched. Confirm the unresolved accounts before finalising this report.",
        "scope_missing": "Report scope is unconfirmed. Verify which accounts this draft should cover.",
        "scope_conflict": "Report scope instructions conflict. Confirm which accounts should be covered.",
        "action_outside_scope": "An action refers to an account outside the confirmed scope. Confirm the intended scope and action.",
        "identity_missing": "Account ownership, platform or type requires confirmation.",
        "valuation_unresolved": "An account valuation could not be selected. The supplied values, dates or qualifications require review.",
        "interpretation_required": "Supporting guidance or unclassified evidence requires adviser review.",
        "secondary_evidence": "Additional source assertions require comparison with the selected instruction.",
        "validation_quote": "An extracted evidence excerpt could not be verified verbatim. Check the cited source; this does not establish that its financial value is wrong.",
        "validation_identity": "Extracted identity or structured fields require checking against their source record.",
        "validation_field_state": "The extraction contains incomplete uncertainty annotations. Verify the affected information against its source.",
    }
    if kind in messages:
        return messages[kind]
    if kind.startswith("validation_"):
        return "Automated review has not confirmed all extracted information. Verify this draft against the sources before proceeding."
    if kind.startswith("extraction_") or kind in {"source_unread"}:
        return "Some source information could not be fully processed. Verify the affected information before proceeding."
    # These are code-owned reconciliation messages, not raw reviewer instructions.
    return issue["message"]


def build_report(facts, specification):
    sections = specification_sections(specification)
    fields = facts.get("request_fields", [])
    request = {}
    for field in fields:
        request.setdefault(field["label"].strip().casefold(), []).append(field["value"])
    blocks = {}
    groups = {
        k: []
        for k in ("scope", "background", "recommendations", "tax", "fees", "closing")
    }

    def add(group, text, observation_ids=(), locked=False):
        key = f"block-{len(blocks) + 1:04d}"
        related = [
            issue
            for issue in facts["issues"]
            if set(observation_ids) & set(issue.get("observation_ids", []))
        ]
        blocks[key] = dict(
            text=text,
            observation_ids=list(observation_ids),
            locked=locked,
            certainty="requires_verification" if related else "as_recorded",
            review_notes=list(dict.fromkeys(review_note(issue) for issue in related)),
        )
        groups[group].append(key)

    included = [a for a in facts["accounts"] if a["in_report_scope"]]
    if included:
        names = [
            " ".join(filter(None, [a["platform"], a["account_type"]])) for a in included
        ]
        prefix = (
            "This draft covers: "
            if facts["scope"].get("complete", True)
            else "Confirmed accounts within the requested scope: "
        )
        add("scope", prefix + "; ".join(names) + ".")
    else:
        intended = request.get("accounts covered", [])
        add(
            "scope",
            "Report scope requires confirmation."
            + (
                " The requested scope is: " + "; ".join(intended) + "."
                if intended
                else ""
            ),
        )
    for pending in facts["scope"].get("unresolved_requests", []):
        ref = pending["reference"]
        description = (
            ref.get("reference_text")
            or " ".join(
                filter(
                    None,
                    [
                        ref.get("owner_text"),
                        ref.get("platform"),
                        ref.get("account_type"),
                    ],
                )
            )
            or "Unspecified account"
        )
        add(
            "scope",
            "Requested "
            + pending["category"]
            + " account not yet confirmed: "
            + description
            + ".",
            [pending["observation_id"]] if pending["observation_id"] else [],
            locked=True,
        )
    if included:
        rows = ["| Account | Owner | Type | Value |", "|---|---|---|---|"]

        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ")

        for account in included:
            value = account.get("valuation")
            amount = money(value["data"]) if value else "Unresolved; requires review"
            if value and value["data"].get("valuation_date"):
                amount += " (as at " + value["data"]["valuation_date"] + ")"
            rows.append(
                "| "
                + " | ".join(
                    cell(v)
                    for v in [
                        account["account_id"] or "Proposed account",
                        ", ".join(account["holders"]) or "Not supplied",
                        " ".join(
                            filter(None, [account["platform"], account["account_type"]])
                        )
                        or "Not supplied",
                        amount,
                    ]
                )
                + " |"
            )
        add("background", "\n".join(rows), locked=True)
    for value in request.get("held in single or joint name?", []):
        add(
            "background", "Requested ownership arrangement: " + value + ".", locked=True
        )
    known_fields = {
        "adviser",
        "accounts covered",
        "agreed risk profile",
        "initial charge",
        "held in single or joint name?",
        "investment amount",
        "source of funds",
        "product recommended",
        "selling existing investments?",
    }
    for label, values in request.items():
        if label not in known_fields:
            for value in values:
                add(
                    "recommendations",
                    "Additional report instruction — " + label + ": " + value,
                    locked=True,
                )
            facts["issues"].append(
                dict(
                    issue_type="request_field_unmapped",
                    message="A report-request field needs adviser interpretation: "
                    + label,
                    observation_ids=[],
                )
            )
    for fact in facts["objectives"]:
        data = fact["data"]
        if data["category"] == "risk_profile":
            wording = " ".join(
                str(data[k])
                for k in ("risk_score", "risk_description")
                if data[k] is not None
            )
            add(
                "background",
                "Recorded risk profile: " + wording + ".",
                fact["observation_ids"],
            )
        else:
            add(
                "background",
                "Recorded circumstances: " + fact["source_text"],
                fact["observation_ids"],
            )
    for label in (
        "investment amount",
        "source of funds",
        "product recommended",
        "selling existing investments?",
    ):
        for value in request.get(label, []):
            add("recommendations", "Report instruction — " + label + ": " + value + ".")
    for fact in facts["actions"]:
        status = fact["data"]["agreement_status"]
        add(
            "recommendations",
            "Recorded action (" + status + "): " + fact["source_text"],
            fact["observation_ids"],
        )
    for fact in facts.get("secondary_assertions", []):
        add(
            "recommendations",
            "Additional recorded assertion (not selected as the primary instruction): "
            + fact["source_text"],
            fact["observation_ids"],
        )
    for fact in facts["funding"]:
        add(
            "recommendations",
            "Recorded funding: " + fact["source_text"],
            fact["observation_ids"],
        )
    for fact in facts["charges"]:
        data = fact["data"]
        value = (
            data["rate_percent"] + "%"
            if data["rate_percent"] is not None
            else money(data)
            if data["amount"] is not None
            else "Not supplied"
        )
        add(
            "fees",
            "Recorded "
            + data["charge_type"].replace("_", " ")
            + " charge: "
            + value
            + ". Status: "
            + data["confirmation_status"].replace("_", " ")
            + ".",
            fact["observation_ids"],
        )
    # A requirement for ongoing fees is not satisfied by an initial-charge figure.
    fee_requirements = " ".join(
        " ".join(s["requirements"] for s in sections).casefold().split()
    )
    for charge_type, pattern in (
        ("platform", r"platform charge"),
        ("ongoing_advice", r"ongoing advice.*charge"),
    ):
        if re.search(pattern, fee_requirements) and not any(
            f["data"]["charge_type"] == charge_type for f in facts["charges"]
        ):
            label = charge_type.replace("_", " ")
            add(
                "fees",
                "Your "
                + label
                + " charge has not been supplied and requires confirmation before implementation.",
                locked=True,
            )
            facts["issues"].append(
                dict(
                    issue_type="confirmation_required",
                    message="Confirm the " + label + " charge before implementation.",
                    observation_ids=[],
                )
            )
    for fact in facts["tax_statements"]:
        add(
            "tax",
            "Recorded tax information: " + fact["source_text"],
            fact["observation_ids"],
        )
    add(
        "closing",
        "Please contact us with any questions. Implementation is subject to adviser review and confirmation of all outstanding information.",
    )

    # Classify specification requirements, not heading names. Unknown instructions
    # remain visible for review instead of being interpreted by the writer.
    patterns = [
        ("closing", r"risk warning|closing line"),
        ("background", r"table of|objectives and circumstances|high.level"),
        ("tax", r"taxable capital gain|disposal|capital gains tax"),
        ("fees", r"ongoing fees|ongoing advice.*charge"),
        ("scope", r"accounts.*report covers|state which.*accounts|FCA.*authorisation"),
        ("recommendations", r"what the client should do|recommendation"),
    ]
    assigned = set()
    selling_values = request.get("selling existing investments?", [])
    selling = (
        True
        if selling_values
        and all(v.strip().lower().startswith("yes") for v in selling_values)
        else False
        if selling_values and all(v.strip().lower() == "no" for v in selling_values)
        else None
    )
    for section in sections:
        requirements = section["requirements"]
        kind = next(
            (k for k, pattern in patterns if re.search(pattern, requirements, re.I)),
            None,
        )
        section["include"] = True
        if re.search(r"\bONLY if\b", requirements, re.I):
            if kind == "tax" and selling is not None:
                section["include"] = selling
            else:
                facts["issues"].append(
                    dict(
                        issue_type="section_condition_unresolved",
                        message="A conditional section requires adviser review: "
                        + section["title"],
                        observation_ids=[],
                    )
                )
        section["block_ids"] = list(groups[kind]) if kind and section["include"] else []
        if kind and section["include"]:
            assigned.update(groups[kind])
        if kind == "tax" and section["include"] and selling:
            # Explicit requirement in the supplied specification; no rates or estimates.
            if re.search(r"annual.*exempt", requirements, re.I):
                add(
                    "tax",
                    "The disposal may create a capital gains tax liability, assessed against the annual exempt amount. The liability requires human confirmation.",
                )
                section["block_ids"] = list(groups["tax"])
                assigned.update(groups["tax"])
        section["fixed_text"] = fixed_wording(requirements)
        if kind is None:
            facts["issues"].append(
                dict(
                    issue_type="section_requirement_unresolved",
                    message="Section requirements need adviser interpretation: "
                    + section["title"],
                    observation_ids=[],
                )
            )
    active = [s for s in sections if s["include"]]
    # Never silently lose facts when a client's template changes.
    remaining = [
        bid
        for ids in groups.values()
        for bid in ids
        if bid not in assigned and not (bid in groups["tax"] and selling is False)
    ]
    if active:
        active[0]["block_ids"] += remaining
    for key in groups["fees"] + groups["closing"] + groups["scope"]:
        blocks[key]["locked"] = True
    notices = []
    for issue in facts["issues"]:
        text = review_note(issue)
        if text not in notices:
            notices.append(text)
    unplaced = list(notices)
    for section in active:
        local = list(
            dict.fromkeys(
                note
                for bid in section["block_ids"]
                for note in blocks[bid]["review_notes"]
            )
        )
        section["review_notes"] = [note for note in local if note in unplaced]
        unplaced = [note for note in unplaced if note not in local]
    if active:
        active[0]["review_notes"] += unplaced
    title_match = re.search(r"^#[ \t]+(.+)$", specification, re.M)
    return dict(
        title=title_match[1].removeprefix("Report specification: ").strip()
        if title_match
        else "Advice Report",
        sections=sections,
        blocks=blocks,
        review_notes=notices,
    )
