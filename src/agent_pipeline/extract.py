"""Run extraction independently of reconciliation and report generation."""

from __future__ import annotations

import argparse
from dataclasses import fields
import os
from pathlib import Path
import sys

from agent_pipeline.extraction_agent import ExtractionAgent, ExtractionSettings, ROOT
from agent_pipeline.extraction_results import ExtractionResultStore


def _create_client():
    from openai import OpenAI

    return OpenAI()


def _load_environment(path: Path):
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=path, override=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract client evidence into structured observations; no report is generated."
    )
    parser.add_argument(
        "--client", required=True, help="Client folder name under --data-dir"
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--instructions", type=Path, default=ROOT / "EXTRACTION_SCHEMA.md"
    )
    parser.add_argument(
        "--schema", type=Path, default=ROOT / "config/extraction_schema.json"
    )
    parser.add_argument(
        "--model",
        help="Overrides EXTRACTION_MODEL, then OPENAI_MODEL; default: gpt-4o-mini",
    )
    defaults = ExtractionSettings(model="placeholder")
    for setting in fields(defaults):
        if setting.name == "model":
            continue
        value = getattr(defaults, setting.name)
        parser.add_argument(
            "--" + setting.name.replace("_", "-"),
            type=type(value),
            default=value,
            help=f"Extraction limit/setting (default: {value})",
        )
    args = parser.parse_args(argv)
    if (
        not args.client
        or args.client in {".", ".."}
        or Path(args.client).name != args.client
        or "\\" in args.client
    ):
        parser.error("--client must be a single folder name")
    data_dir = args.data_dir.resolve()
    client_dir = (data_dir / args.client).resolve()
    if not client_dir.is_relative_to(data_dir) or not client_dir.is_dir():
        parser.error("Client must be an existing directory inside --data-dir")
    _load_environment(args.env_file)
    model = (
        args.model
        or os.environ.get("EXTRACTION_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    )
    try:
        settings = ExtractionSettings(
            model=model,
            **{
                f.name: getattr(args, f.name)
                for f in fields(defaults)
                if f.name != "model"
            },
        )
    except ValueError as error:
        parser.error(str(error))
    client = None
    try:
        store = ExtractionResultStore(args.output_dir, schema_path=args.schema)
        client = _create_client()
        agent = ExtractionAgent(
            client,
            settings,
            instructions_path=args.instructions,
            schema_path=args.schema,
        )
        print(f"Extracting {args.client} using {settings.model}...", flush=True)
        run = agent.run(client_dir)
        saved = store.save(args.client, run, settings)
        print(f"Extraction: {saved.state}")
        if saved.result_path:
            print(f"Result: {saved.result_path}")
        print(f"Manifest: {saved.current_manifest_path}")
        print(f"Model calls: {run.model_calls}; tool calls: {run.tool_calls}")
        if saved.state != "complete":
            print(
                "Partial extraction; inspect the manifest and diagnostics.",
                file=sys.stderr,
            )
            return 1
        return 0
    except Exception as error:
        # SDK error bodies can contain credentials or submitted content.
        print(
            f"Extraction could not finish ({type(error).__name__}). "
            "Check dependencies, API configuration and input/output paths.",
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            close = getattr(client, "close", None)
            if close:
                close()


if __name__ == "__main__":
    raise SystemExit(main())
