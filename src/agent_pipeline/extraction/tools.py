"""All model-facing tool descriptions and argument schemas."""

from copy import deepcopy

_DESCRIPTIONS = {
    "list_files": (
        "Call first. Lists every file in the selected client folder with its "
        "code-assigned source_id, filename and format, and identifies missing "
        "required files. This does not read file content."
    ),
    "read_source": (
        "Read a file using its exact source_id from list_files. Read "
        "report_request.docx and template_spec.md first, then all remaining sources. "
        "Code automatically chooses the JSON, Word/Markdown or image reader from "
        "the file format. Returns source content or an explicit "
        "reading limitation. Images are supplied for visual extraction. "
        "Do not choose a reader or provide a filesystem path."
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
            "properties": {}
            if name == "list_files"
            else {
                "source_id": {
                    "type": "string",
                    "description": "Exact ID from list_files.",
                }
            },
            "required": [] if name == "list_files" else ["source_id"],
            "additionalProperties": False,
        },
    }
    for name, description in _DESCRIPTIONS.items()
]


def validation_definition(extraction_schema):
    schema = deepcopy(extraction_schema)
    definitions = schema.pop("$defs")
    schema.pop("$schema", None)
    return {
        "name": "validate_extraction",
        "description": "After reading all sources, submit the complete candidate extraction. "
        "Runs deterministic checks and an independent evidence review. Repair errors "
        "and resubmit within the validation budget. If findings remain or the budget "
        "is exhausted, return the best candidate; findings become report review notes. Review observation "
        "IDs are code-generated and end in the candidate's one-based observation index.",
        "parameters": {
            "type": "object",
            "properties": {"candidate": schema},
            "required": ["candidate"],
            "additionalProperties": False,
            "$defs": definitions,
        },
    }
