"""Deterministic checks on extraction structure and source support."""

import re

from jsonschema import ValidationError


def normalise_text(text):
    return " ".join(text.split())


def _finding(category, message, observation=None, source_id=None):
    return {
        "category": category,
        "severity": "error",
        "message": message,
        "observation_ids": [observation["observation_id"]] if observation else [],
        "source_ids": [source_id] if source_id else [],
        "evidence": [],
    }


def _field_value(data, path):
    if not re.fullmatch(
        r"[A-Za-z_]\w*(?:\[\d+\])?(?:\.[A-Za-z_]\w*(?:\[\d+\])?)*", path
    ):
        raise ValueError("Invalid field path")
    value = data
    for name, index in re.findall(r"([A-Za-z_]\w*)(?:\[(\d+)\])?", path):
        value = value[name]
        if index:
            value = value[int(index)]
    return value


def _null_paths(value, prefix=""):
    if value is None:
        yield prefix
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _null_paths(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _null_paths(child, f"{prefix}[{i}]")


def _account_ids(value):
    if isinstance(value, dict):
        if value.get("account_id"):
            yield value["account_id"]
        for child in value.values():
            yield from _account_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _account_ids(child)


def check_extraction(extraction, evidence, validator):
    findings = []
    try:
        validator.validate(extraction)
    except ValidationError:
        return [_finding("schema", "Extraction does not match the saved-result schema")]
    sources = [s["source_id"] for s in extraction["sources"]]
    if len(sources) != len(set(sources)):
        findings.append(_finding("references", "Duplicate source IDs"))
    ids = [o["observation_id"] for o in extraction["observations"]]
    if len(ids) != len(set(ids)):
        findings.append(_finding("references", "Duplicate observation IDs"))
    issue_map = {issue["issue_id"]: issue for issue in extraction["extraction_issues"]}
    if len(issue_map) != len(extraction["extraction_issues"]):
        findings.append(_finding("references", "Duplicate issue IDs"))
    for source in extraction["sources"]:
        if source["read_status"] != "read":
            findings.append(
                _finding(
                    "coverage",
                    "Extraction source was not fully read",
                    source_id=source["source_id"],
                )
            )
    for observation in extraction["observations"]:
        sid = observation["source_id"]
        source = evidence.get(sid)
        if sid not in sources or source is None:
            findings.append(
                _finding(
                    "references",
                    "Observation names an unknown source",
                    observation,
                    sid,
                )
            )
            continue
        text = source["text"]
        quote = observation["source_text"]
        if not quote.strip() or (
            text is not None and normalise_text(quote) not in normalise_text(text)
        ):
            findings.append(
                _finding(
                    "quote",
                    "Supporting text is not a verbatim excerpt of the cited document",
                    observation,
                    sid,
                )
            )
        if text is not None:
            for account_id in _account_ids(observation["data"]):
                if not re.search(
                    r"(?<![\w-])" + re.escape(account_id) + r"(?![\w-])", text
                ):
                    findings.append(
                        _finding(
                            "identity",
                            "Account ID is not stated in the cited document",
                            observation,
                            sid,
                        )
                    )
        states = {}
        for entry in observation["field_states"]:
            path, state = entry["field"], entry["state"]
            if path in states:
                findings.append(
                    _finding(
                        "field_state",
                        "Repeated field state: " + path,
                        observation,
                        sid,
                    )
                )
            states[path] = state
            try:
                value = _field_value(observation["data"], path)
            except (ValueError, KeyError, IndexError, TypeError):
                findings.append(
                    _finding(
                        "field_state",
                        "Field state path must resolve relative to data: " + path,
                        observation,
                        sid,
                    )
                )
                continue
            if value is not None and value != []:
                findings.append(
                    _finding(
                        "field_state",
                        "Unknown/missing state contradicts populated field: " + path,
                        observation,
                        sid,
                    )
                )
            if state == "qualified_value":
                parent = path.rsplit(".", 1)[0] if "." in path else None
                container = (
                    _field_value(observation["data"], parent)
                    if parent
                    else observation["data"]
                )
                if not path.endswith("amount") or container.get("precision") not in {
                    "approximate",
                    "range",
                    "upper_bound",
                    "lower_bound",
                }:
                    findings.append(
                        _finding(
                            "field_state",
                            "qualified_value requires an approximate or bounded amount",
                            observation,
                            sid,
                        )
                    )
        for path in _null_paths(observation["data"]):
            if path not in states:
                findings.append(
                    _finding(
                        "field_state",
                        "Null field lacks an explicit state: " + path,
                        observation,
                        sid,
                    )
                )
    by_id = {o["observation_id"]: o for o in extraction["observations"]}
    for issue in extraction["extraction_issues"]:
        if issue["source_id"] not in sources:
            findings.append(_finding("references", "Issue names an unknown source"))
        for oid in issue["observation_ids"]:
            if oid not in by_id:
                findings.append(
                    _finding("references", "Issue references an unknown observation")
                )
    return findings
