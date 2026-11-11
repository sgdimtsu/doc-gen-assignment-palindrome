"""Structural checks and advisory validation status."""

import hashlib
import json


def candidate_hash(candidate):
    return hashlib.sha256(
        json.dumps(
            candidate, sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def validation_state(candidate, reports, result=None):
    if not reports:
        return "not_reviewed"
    report = reports[-1]
    if report.get("state") == "not_reviewed" and "extraction_sha256" not in report:
        return "not_reviewed"
    if candidate is not None and report.get("candidate_sha256") != candidate_hash(
        candidate
    ):
        return "stale"
    if result is not None:
        digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        if digest != report.get("extraction_sha256"):
            return "stale" if report.get("extraction_sha256") else "incomplete"
    if report.get("errors") or not report.get("evidence_review_complete"):
        return "incomplete"
    findings = report.get("deterministic_findings", []) + report.get(
        "review_findings", []
    )
    if report.get("state") == "failed" or any(
        f.get("severity") == "error" for f in findings
    ):
        return "failed"
    return "passed" if report.get("state") == "passed" else "incomplete"


def candidate_errors(candidate, inventory):
    errors = []
    known = {s["source_id"] for s in inventory}
    ids = [s["source_id"] for s in candidate["sources"]]
    if len(known) != len(inventory) or len(ids) != len(set(ids)) or set(ids) != known:
        errors.append("Source classifications must cover the inventory exactly once")
    for collection in ("observations", "extraction_issues"):
        for index, record in enumerate(candidate[collection]):
            if record["source_id"] not in known:
                errors.append(f"{collection}[{index}]: unknown source ID")
            if collection == "extraction_issues":
                links = record["observation_indexes"]
                if len(links) != len(set(links)) or any(
                    i < 0 or i >= len(candidate["observations"]) for i in links
                ):
                    errors.append(f"{collection}[{index}]: invalid observation indexes")
    return errors


def execution_errors(inventory):
    errors = []
    if not {"report_request.docx", "template_spec.md"}.issubset(
        {s["filename"] for s in inventory if s["read_status"] == "read"}
    ):
        errors.append("Required relevance files have not been read")
    errors.extend(
        "Incomplete reading coverage: " + s["source_id"]
        for s in inventory
        if s["read_status"] != "read"
    )
    return errors
