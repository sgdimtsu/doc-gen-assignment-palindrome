from agent_pipeline.evaluate import evaluate


def test_report_checks_detect_missing_required_and_present_forbidden_text():
    checks = [
        {"name": "risk", "pattern": "risk 4"},
        {"name": "internal references", "pattern": r"\[Issue \d", "present": False},
    ]
    assert all(r["passed"] for r in evaluate("Agreed risk 4.", checks))
    assert not any(r["passed"] for r in evaluate("[Issue 0]", checks))
