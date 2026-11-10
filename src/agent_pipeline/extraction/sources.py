"""Client-scoped source readers. No model calls or reconciliation occur here."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZipFile


REQUIRED_FILES = ("report_request.docx", "template_spec.md")
FORMATS = {
    ".json": "json",
    ".docx": "docx",
    ".md": "markdown",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text(element: ET.Element) -> str:
    """Preserve visible text, tabs and line breaks in a Word paragraph."""
    return "".join(
        node.text or ""
        if node.tag == W + "t"
        else "\t"
        if node.tag == W + "tab"
        else "\n"
        if node.tag in {W + "br", W + "cr"}
        else ""
        for node in element.iter()
    )


class SourceTools:
    """Inventory a fixed folder and read sources by code-assigned ID.

    Reading status means content was returned, not understood by a model.
    The future agent runtime must track delivery and reported legibility too.
    """

    def __init__(self, client_dir: Path):
        self.root = client_dir.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Client path must be a directory")
        self.sources = {}
        self._paths = {}
        for index, path in enumerate(sorted(self.root.rglob("*")), start=1):
            if path.is_file():
                source_id = f"source-{index:04d}"
                self._paths[source_id] = path
                self.sources[source_id] = {
                    "source_id": source_id,
                    "filename": path.relative_to(self.root).as_posix(),
                    "format": FORMATS.get(path.suffix.lower(), "unsupported"),
                    "read_status": "pending",
                }

    def list_files(self) -> dict:
        return {
            "sources": [dict(s) for s in self.sources.values()],
            "missing_required_files": [
                name for name in REQUIRED_FILES if not (self.root / name).is_file()
            ],
        }

    def _path(self, source_id: str, formats: set[str]) -> Path:
        if source_id not in self.sources:
            raise ValueError("Unknown source ID")
        path = self._paths[source_id].resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise ValueError("Source resolves outside the client folder")
        source = self.sources[source_id]
        if source["filename"] not in REQUIRED_FILES:
            read = {
                s["filename"]
                for s in self.sources.values()
                if s["read_status"] == "read"
            }
            if not set(REQUIRED_FILES).issubset(read):
                raise ValueError("Read both required relevance files first")
        if source["format"] not in formats:
            raise ValueError("Wrong reader or unsupported source format")
        return path

    def _read(self, source_id: str, formats: set[str], reader) -> dict:
        reading_started = False
        try:
            path = self._path(source_id, formats)
            reading_started = True
            content = reader(path)
        except Exception as error:
            # Failed requests do not erase a previous successful read.
            if (
                reading_started
                and source_id in self.sources
                and self.sources[source_id]["read_status"] != "read"
            ):
                self.sources[source_id]["read_status"] = (
                    "unsupported"
                    if self.sources[source_id]["format"] == "unsupported"
                    else "failed"
                )
            result = {
                "source_id": source_id,
                "ok": False,
                "error": type(error).__name__ + ": " + str(error),
            }
        else:
            complete = not content.get("limitations")
            self.sources[source_id]["read_status"] = "read" if complete else "partial"
            result = {
                "source_id": source_id,
                "ok": True,
                "complete": complete,
                **content,
            }
        return result

    def read_source(self, source_id: str) -> dict:
        source = self.sources.get(source_id)
        if source is None:
            return self._read(source_id, set(), lambda path: {})
        reader = {
            "json": self.read_json,
            "docx": self.read_document,
            "markdown": self.read_document,
            "png": self.inspect_image,
            "jpeg": self.inspect_image,
        }.get(source["format"])
        if reader is None:
            source["read_status"] = "unsupported"
            return self._read(source_id, set(), lambda path: {})
        return reader(source_id)

    def read_json(self, source_id: str) -> dict:
        def read(path):
            def reject_constant(value):
                raise ValueError(f"Invalid JSON constant: {value}")

            def unique_keys(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"Duplicate JSON key: {key}")
                    result[key] = value
                return result

            text = path.read_text(encoding="utf-8")
            data = json.loads(
                text, parse_constant=reject_constant, object_pairs_hook=unique_keys
            )
            return {
                "data": data,
                "raw_text": text,
            }

        return self._read(source_id, {"json"}, read)

    def read_document(self, source_id: str) -> dict:
        def read(path):
            if path.suffix.lower() == ".md":
                return {"text": path.read_text(encoding="utf-8")}
            with ZipFile(path) as archive:
                root = ET.fromstring(archive.read("word/document.xml"))
                body = root.find(W + "body")
                if body is None:
                    raise ValueError("Word document has no body")
                blocks = []
                limitations = []
                for child in body:
                    if child.tag == W + "p":
                        blocks.append({"text": _text(child)})
                    elif child.tag == W + "tbl":
                        for element in child.findall(W + "tr"):
                            blocks.append(
                                {
                                    "cells": [
                                        "\n".join(
                                            _text(p) for p in cell.findall(W + "p")
                                        )
                                        for cell in element.findall(W + "tc")
                                    ],
                                }
                            )
                        if child.findall(".//" + W + "tbl"):
                            limitations.append(
                                "Nested table content requires additional reading"
                            )
                    elif child.tag != W + "sectPr":
                        limitations.append(
                            "Unsupported Word body element: " + child.tag
                        )
                for name in archive.namelist():
                    if name.startswith(
                        ("word/header", "word/footer", "word/media/")
                    ) or name in {"word/footnotes.xml", "word/endnotes.xml"}:
                        limitations.append(
                            "Additional Word content not extracted: " + name
                        )
                if root.findall(".//" + W + "txbxContent"):
                    limitations.append("Text boxes require additional reading")
                return {
                    "blocks": blocks,
                    "limitations": limitations,
                }

        return self._read(source_id, {"markdown", "docx"}, read)

    def inspect_image(self, source_id: str) -> dict:
        def read(path):
            data = path.read_bytes()
            fmt = self.sources[source_id]["format"]
            if (fmt == "png" and not data.startswith(b"\x89PNG\r\n\x1a\n")) or (
                fmt == "jpeg" and not data.startswith(b"\xff\xd8")
            ):
                raise ValueError("Image signature does not match its format")
            return {
                "image": {
                    "type": "input_image",
                    "detail": "high",
                    "image_url": f"data:image/{fmt};base64,"
                    + base64.b64encode(data).decode("ascii"),
                },
            }

        return self._read(source_id, {"png", "jpeg"}, read)
