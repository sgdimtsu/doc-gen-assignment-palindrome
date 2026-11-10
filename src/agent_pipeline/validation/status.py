"""Match a code-owned validation verdict to the exact candidate."""

import hashlib
import json


def candidate_hash(candidate):
    return hashlib.sha256(
        json.dumps(
            candidate, sort_keys=True, ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def validation_errors(candidate, reports):
    if not reports:
        return ["Final candidate has not been validated"]
    report = reports[-1]
    if candidate is None or report.get("candidate_sha256") != candidate_hash(candidate):
        return ["Final candidate differs from the last validated candidate"]
    findings = report.get("deterministic_findings", []) + report.get(
        "review_findings", []
    )
    if (
        report.get("state") != "passed"
        or not report.get("evidence_review_complete")
        or report.get("errors")
        or any(f.get("severity") == "error" for f in findings)
    ):
        return ["Final candidate has unresolved or incomplete validation"]
    return []


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


def release_errors(candidate, reports, inventory, result=None):
    errors = validation_errors(candidate, reports)
    if not inventory or not {"report_request.docx", "template_spec.md"}.issubset(
        {s["filename"] for s in inventory if s["read_status"] == "read"}
    ):
        errors.append("Required relevance files have not been read")
    errors.extend(
        "Incomplete reading coverage: " + s["source_id"]
        for s in inventory
        if s["read_status"] != "read"
    )
    if candidate:
        errors.extend(
            "Reported incomplete reading: " + issue["source_id"]
            for issue in candidate.get("extraction_issues", [])
            if issue["issue_type"] in {"unreadable_content", "unsupported_format"}
        )
    if not errors and result is not None:
        digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        if digest != reports[-1].get("extraction_sha256"):
            errors.append("Saved extraction differs from the reviewed extraction")
    return errors
