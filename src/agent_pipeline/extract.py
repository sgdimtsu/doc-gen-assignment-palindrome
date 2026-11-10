"""Run extraction and validation independently of report generation."""

import argparse
import asyncio
from pathlib import Path
import sys

from openai import AsyncOpenAI

from agent_pipeline.cli import client_directory, extraction_model, load_environment
from agent_pipeline import ROOT
from agent_pipeline.extraction.agent import ExtractionAgent
from agent_pipeline.extraction.models import ExtractionSettings
from agent_pipeline.extraction.results import save_run
from agent_pipeline.validation.status import validation_state


def _create_client():
    return AsyncOpenAI()


def main(argv=None):
    return asyncio.run(_main(argv))


async def _main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True, help="Client folder under data/")
    parser.add_argument("--model", help="Overrides EXTRACTION_MODEL, then OPENAI_MODEL")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    args = parser.parse_args(argv)
    client_dir = client_directory(parser, args.client, ROOT / "data")
    load_environment()
    settings = ExtractionSettings(model=extraction_model(args.model, "gpt-4o-mini"))
    client = None
    try:
        client = _create_client()
        print(f"Extracting {args.client} using {settings.model}...", flush=True)
        run = await ExtractionAgent(client, settings).run(client_dir)
        folder = save_run(args.output_dir, args.client, run, settings)
        print(f"Extraction: {run.state}")
        print(f"Run directory: {folder}")
        print(f"Model calls: {run.model_calls}; tool calls: {run.tool_calls}")
        print(f"Validation attempts: {len(run.validation_reports)}")
        print(
            f"Validation: {validation_state(run.response, run.validation_reports)} (advisory)"
        )
        if run.state != "complete":
            for error in run.errors:
                print(error, file=sys.stderr)
            print(
                "Partial extraction; inspect summary.json and validation.json in this run directory.",
                file=sys.stderr,
            )
            return 1
        return 0
    except Exception as error:
        # Avoid raw SDK messages and bodies, which may contain credentials or source content.
        print("Extraction could not finish: " + type(error).__name__, file=sys.stderr)
        if getattr(error, "status_code", None) is not None:
            print("API HTTP status: " + str(error.status_code), file=sys.stderr)
        return 1
    finally:
        if client is not None:
            await client.close()


if __name__ == "__main__":
    raise SystemExit(main())
