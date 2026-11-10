"""Run extraction and validation independently of report generation."""

import argparse
import asyncio
import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent_pipeline import ROOT
from agent_pipeline.extraction.agent import ExtractionAgent
from agent_pipeline.extraction.models import ExtractionSettings
from agent_pipeline.extraction.results import save_run


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
    if (
        not args.client
        or args.client in {".", ".."}
        or Path(args.client).name != args.client
        or "\\" in args.client
    ):
        parser.error("--client must be a single folder name")
    client_dir = (ROOT / "data" / args.client).resolve()
    if (
        not client_dir.is_relative_to((ROOT / "data").resolve())
        or not client_dir.is_dir()
    ):
        parser.error("Client must be an existing directory inside data/")
    load_dotenv(ROOT / ".env", override=False)
    settings = ExtractionSettings(
        model=args.model
        or os.environ.get("EXTRACTION_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    )
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
