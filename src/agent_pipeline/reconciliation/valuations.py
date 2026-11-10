"""Choose a dated current valuation without converting qualifications to exact values."""

from datetime import date
from decimal import Decimal, InvalidOperation


def select_valuation(observations):
    if not observations:
        return None, "No valuation was extracted"
    currencies = {o["data"]["currency"] for o in observations if o["data"]["currency"]}
    if len(currencies) > 1:
        return None, "Valuation currencies disagree; no conversion was inferred"
    dated = []
    for observation in observations:
        value_date = observation["data"]["valuation_date"]
        try:
            dated.append((date.fromisoformat(value_date), observation))
        except (ValueError, TypeError):
            return (
                None,
                "Valuation dates are missing or invalid; no latest value was selected",
            )
    latest = max(d for d, _ in dated)
    candidates = [o for d, o in dated if d == latest]

    # Exact decimal spelling may differ; preserve the selected source spelling.
    def signature(observation):
        data = observation["data"]
        amount = data["amount"]
        if amount is not None:
            try:
                amount = Decimal(amount)
                if not amount.is_finite():
                    raise ValueError("Non-finite amount")
            except (InvalidOperation, ValueError):
                return None
        return (
            amount,
            data["currency"],
            data["precision"],
            (None if data["precision"] == "exact" else data["amount_text"]),
        )

    signatures = [signature(o) for o in candidates]
    if None in signatures or len(set(signatures)) != 1:
        return None, "Latest dated valuations disagree; no value was selected"
    if any(
        s["state"] in {"ambiguous", "unreadable"}
        and s["field"]
        in {"amount", "amount_text", "valuation_date", "currency", "precision"}
        for o in candidates
        for s in o["field_states"]
    ):
        return None, "Latest valuation has unresolved uncertainty"
    data = candidates[0]["data"]
    if data["amount"] is None and data["precision"] not in {
        "approximate",
        "range",
        "upper_bound",
        "lower_bound",
    }:
        return None, "Latest valuation is missing; an older value was not substituted"
    if data["amount"] is None and not data["amount_text"]:
        return None, "Latest valuation has no stated amount"
    return candidates, None
