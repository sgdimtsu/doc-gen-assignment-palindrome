"""Copy supported structured evidence; never ask a model to recreate these records."""

from copy import deepcopy
import json
import re


def null_states(value, prefix=""):
    if value is None:
        return [{"field": prefix, "state": "not_stated"}]
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            result += null_states(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result += null_states(child, f"{prefix}[{index}]")
    return result


def observation(kind, sid, text, data, sections):
    return dict(
        observation_type=kind,
        source_id=sid,
        source_text=text,
        evidence_scope="client_specific",
        data=data,
        sections=sections,
        field_states=null_states(data),
    )


def account_records(data, sid):
    if not isinstance(data, dict) or not isinstance(data.get("holders"), dict):
        return None
    records = []
    for holder in data["holders"].values():
        if not isinstance(holder, dict) or not isinstance(holder.get("accounts"), list):
            return None
        for account in holder["accounts"]:
            if not isinstance(account, dict) or not isinstance(
                account.get("account_id"), str
            ):
                return None
            if account.get("status") not in {
                None,
                "open",
                "closed",
                "proposed",
                "unclear",
            }:
                return None
            if any(
                account.get(k) is not None and not isinstance(account[k], str)
                for k in (
                    "platform",
                    "type",
                    "owner",
                    "currency",
                    "valuation_date",
                    "value",
                )
            ):
                return None
            ref = dict(
                account_id=account["account_id"],
                platform=account.get("platform"),
                account_type=account.get("type"),
                owner_text=account.get("owner"),
                reference_text=account["account_id"],
            )
            text = json.dumps(account, ensure_ascii=False)
            people = (
                [{"name": holder["name"], "reference_text": holder["name"]}]
                if holder.get("name")
                else []
            )
            records.append(
                observation(
                    "account",
                    sid,
                    text,
                    dict(account=ref, holders=people, status=account.get("status")),
                    ["background_objectives"],
                )
            )
            records.append(
                observation(
                    "valuation",
                    sid,
                    text,
                    dict(
                        account=deepcopy(ref),
                        amount=account.get("value"),
                        amount_text=account.get("value") or "Not supplied",
                        currency=account.get("currency"),
                        valuation_date=account.get("valuation_date"),
                        date_text=account.get("valuation_date"),
                        precision="exact",
                    ),
                    ["background_objectives"],
                )
            )
    return records


def request_records(blocks, sid):
    fields, records = [], []
    for block in blocks:
        cells = block.get("cells", [])
        if len(cells) != 2:
            continue
        label, value = cells
        fields.append(dict(source_id=sid, label=label, value=value))
        text = label + "\n" + value
        if label.strip().casefold() == "agreed risk profile":
            match = re.fullmatch(r"\s*(\d+)\s*\((.+)\)\s*", value)
            records.append(
                observation(
                    "objective_circumstance",
                    sid,
                    text,
                    dict(
                        people=[],
                        category="risk_profile",
                        timeframe_text=None,
                        risk_score=match[1] if match else None,
                        risk_description=match[2] if match else value,
                    ),
                    ["background_objectives", "recommendations"],
                )
            )
        if label.strip().casefold() == "initial charge" and re.fullmatch(
            r"\d+(?:\.\d+)?%", value.strip()
        ):
            records.append(
                observation(
                    "charge",
                    sid,
                    text,
                    dict(
                        charge_type="initial",
                        account=None,
                        platform=None,
                        rate_percent=value.strip()[:-1],
                        amount=None,
                        currency=None,
                        basis_text=label,
                        frequency_text=None,
                        confirmation_status="explicitly_confirmed",
                    ),
                    ["fees_charges"],
                )
            )
    return fields, records


def merge_candidate(candidate, protected, roles, request_fields):
    """Replace protected source observations and remap model issue links safely."""
    result = deepcopy(candidate)
    kept, remap = [], {}
    request_ids = {f["source_id"] for f in request_fields}
    for index, obs in enumerate(result["observations"]):
        sid, kind = obs["source_id"], obs["observation_type"]
        if roles.get(sid) in {"account_records", "unknown"} and kind not in {
            "unclassified",
            "instruction",
        }:
            continue
        if sid in request_ids and (
            kind == "valuation"
            or (
                kind == "objective_circumstance"
                and obs["data"]["category"] == "risk_profile"
            )
            or (kind == "charge" and obs["data"]["charge_type"] == "initial")
        ):
            continue

        # Ownership arrangement labels are not personal names.
        def clean(value):
            if isinstance(value, dict):
                if isinstance(value.get("owner_text"), str) and value[
                    "owner_text"
                ].casefold() in {"single", "joint clients", "individual"}:
                    value["owner_text"] = None
                for child in value.values():
                    clean(child)
            elif isinstance(value, list):
                for child in value:
                    clean(child)

        clean(obs["data"])
        existing = {s["field"] for s in obs["field_states"]}
        obs["field_states"] += [
            s
            for s in null_states(obs["data"])
            if s["field"].endswith("owner_text") and s["field"] not in existing
        ]
        remap[index] = len(kept)
        kept.append(obs)
    for issue in result["extraction_issues"]:
        issue["observation_indexes"] = [
            remap[i] for i in issue["observation_indexes"] if i in remap
        ]
    for sid, role in roles.items():
        if role == "unknown":
            result["extraction_issues"].append(
                dict(
                    issue_type="schema_gap",
                    source_id=sid,
                    affected_fields=[],
                    description="Unsupported JSON layout: no account mapping was inferred.",
                    observation_indexes=[],
                )
            )
    result["observations"] = kept + deepcopy(protected)
    for source in result["sources"]:
        if source["source_id"] in roles:
            source["role"] = roles[source["source_id"]]
    return result
