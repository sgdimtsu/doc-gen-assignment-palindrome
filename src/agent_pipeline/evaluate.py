"""Check saved report text against a small, manually authored acceptance checklist."""

import argparse
import json
from pathlib import Path
import re


def evaluate(report, checks):
    return [
        {
            "check": check["name"],
            "passed": bool(re.search(check["pattern"], report, re.I | re.M))
            == check.get("present", True),
        }
        for check in checks
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--checks", type=Path, required=True)
    args = parser.parse_args(argv)
    checks = json.loads(args.checks.read_text())
    results = evaluate(args.report.read_text(), checks)
    print(json.dumps(results, indent=2))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
