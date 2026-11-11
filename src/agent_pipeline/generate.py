"""Generate a draft report: extract, reconcile, then write using the client specification."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from openai import AsyncOpenAI, OpenAI

from agent_pipeline.cli import client_directory, extraction_model, load_environment
from agent_pipeline import ROOT
from agent_pipeline.extraction.agent import ExtractionAgent
from agent_pipeline.extraction.models import ExtractionSettings
from agent_pipeline.extraction.results import save_run, write_json
from agent_pipeline.reconciliation.engine import reconcile
from agent_pipeline.reconciliation.input import load_extraction
from agent_pipeline.writing.writer import WriterOutputError, write_report


def main(argv=None):
    return asyncio.run(_main(argv))


async def _main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True, help="Client folder under data/")
    parser.add_argument(
        "--run-dir", type=Path, help="Reuse a saved run instead of extracting again"
    )
    parser.add_argument("--model", help="Override extraction and writer models")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config/template_config_v2.json"
    )
    args = parser.parse_args(argv)
    client_dir = client_directory(parser, args.client, args.data_dir)
    load_environment()
    try:
        specification = (client_dir / "template_spec.md").read_text(encoding="utf-8")
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if args.run_dir:
            summary = json.loads((args.run_dir / "summary.json").read_text())
            if summary["client"] != args.client:
                parser.error("Run belongs to a different client")
    except (OSError, ValueError, KeyError, StopIteration):
        parser.error(
            "Cannot load client specification, report request, V2 config or selected run"
        )

    model = extraction_model(args.model)
    writer_model = args.model or os.environ.get("WRITER_MODEL") or model
    folder = args.run_dir
    try:
        if folder is None:
            settings = ExtractionSettings(model=model)
            print(f"Extracting {args.client} using {model}...", flush=True)
            client = AsyncOpenAI()
            try:
                run = await ExtractionAgent(client, settings).run(client_dir)
            finally:
                await client.close()
            folder = save_run(args.output_dir, args.client, run, settings)
            print(f"Extraction: {run.state}; run directory: {folder}", flush=True)
        # Rebuild from this run's extraction so cached reconciliation cannot bypass
        # the current boundary checks or mix in current raw client documents.
        extraction, reports, errors = load_extraction(folder)
        facts = reconcile(extraction, reports, errors, specification)
        write_json(folder / "reconciliation.json", facts)
        print(
            f"Writing draft using {writer_model}; review issues: {len(facts.get('issues', []))}...",
            flush=True,
        )
        with OpenAI(timeout=90, max_retries=2) as client:
            report = write_report(
                client,
                writer_model,
                facts,
                config,
                response_path=folder / "writer_response.json",
            )
        path = folder / "report.md"
        path.write_text(report, encoding="utf-8")
        print(f"Wrote draft: {path}")
        return 0
    except WriterOutputError as error:
        parser.exit(
            1, f"Report generation failed: {error}; inspect run directory {folder}\n"
        )
    except Exception as error:
        # Raw SDK messages/bodies may contain credentials or submitted source content.
        parser.exit(
            1,
            "Report generation failed: "
            + type(error).__name__
            + (f"; inspect run directory {folder}" if folder else "")
            + "\n",
        )


if __name__ == "__main__":
    raise SystemExit(main())
