"""Model-facing tool definitions; execution stays in SourceTools."""

_DESCRIPTIONS = {
    "list_files": (
        "Call first. Lists every file in the selected client folder with its "
        "code-assigned source_id, filename and format, and identifies missing "
        "required files. This does not read file content."
    ),
    "read_document": (
        "Read a docx or markdown source using its inventory source_id. Read "
        "report_request.docx and template_spec.md before other sources. Returns "
        "Word paragraphs and table rows in document order, or Markdown lines, "
        "with locations. Reports unsupported or partial content explicitly."
    ),
    "read_json": (
        "After both relevance files are read, read a json source by source_id. "
        "Returns parsed structure, raw text and JSON Pointer locations, preserving "
        "nulls and zero. Does not match accounts or choose authoritative facts."
    ),
    "inspect_image": (
        "After both relevance files are read, inspect a png or jpeg by source_id. "
        "Returns the image for visual extraction with a full_image reference. "
        "Report unreadable regions as extraction issues. Loading the image "
        "does not establish that every detail was legible."
    ),
}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {} if name == "list_files" else {
                "source_id": {"type": "string", "description": "Exact ID from list_files."}
            },
            "required": [] if name == "list_files" else ["source_id"],
            "additionalProperties": False,
        },
    }
    for name, description in _DESCRIPTIONS.items()
]
