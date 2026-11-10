"""Shared setup for the extraction and report commands."""

import os

from dotenv import load_dotenv

from agent_pipeline import ROOT


def load_environment():
    load_dotenv(ROOT / ".env", override=False)


def client_directory(parser, name, data_dir):
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        parser.error("--client must be a single folder name")
    data_dir = data_dir.resolve()
    directory = (data_dir / name).resolve()
    if not directory.is_relative_to(data_dir) or not directory.is_dir():
        parser.error("Client must be an existing directory inside --data-dir")
    return directory


def extraction_model(override, default="gpt-4.1"):
    return (
        override
        or os.environ.get("EXTRACTION_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or default
    )
