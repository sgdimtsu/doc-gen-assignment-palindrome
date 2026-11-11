"""Reconcile an extraction run with advisory review notes; no model calls or report generation."""

import argparse
from pathlib import Path

from agent_pipeline import ROOT
from agent_pipeline.extraction.results import write_json
import json
from agent_pipeline.reconciliation.input import load_extraction
from agent_pipeline.reconciliation.engine import reconcile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        extraction, reports, errors = load_extraction(args.run_dir)
        summary = json.loads((args.run_dir / "summary.json").read_text())
        spec_path = ROOT / "data" / summary["client"] / "template_spec.md"
        specification = spec_path.read_text() if spec_path.is_file() else None
        result = reconcile(extraction, reports, errors, specification)
        path = args.run_dir / "reconciliation.json"
        write_json(path, result)
    except (OSError, ValueError) as error:
        parser.exit(1, "Reconciliation could not finish: " + str(error) + "\n")
    print(
        "Reconciliation: draft requires review"
        if result["requires_review"]
        else "Reconciliation: draft"
    )
    print("Result: " + str(path))
    print(f"Issues: {len(result['issues'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
