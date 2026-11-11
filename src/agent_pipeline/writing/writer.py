"""Write grounded narrative while code preserves protected facts and review notices."""

import json
import re
from copy import deepcopy
from collections import Counter

from jsonschema import Draft202012Validator, ValidationError

from document_formatter.formatting import format_document
from agent_pipeline.reconciliation.report import FCA_LINE


class WriterOutputError(ValueError):
    pass


# These substitutions are presentation only: the original strings remain code-owned.
VALUE = re.compile(r"(?:[£$€]\s*)?\d[\d,]*(?:\.\d+)?(?:%|(?:[-/]\d+)+)?")


def protect_values(text):
    values = {}

    def replace(match):
        key = "{{value-" + str(len(values) + 1) + "}}"
        values[key] = match[0]
        return key

    return VALUE.sub(replace, text), values


def restore_values(wording, original):
    _, values = protect_values(original)
    if not wording.strip() or re.search(
        r"^#{1,2}\s|REVIEW REQUIRED|\[Issue", wording, re.I | re.M
    ):
        raise WriterOutputError("Narrative contains report structure or internal notes")
    tokens = re.findall(r"\{\{value-\d+\}\}", wording)
    if any(token not in values for token in tokens):
        raise WriterOutputError("Narrative introduced a protected-value token")
    for token, value in values.items():
        wording = wording.replace(token, value)
    # Accept tokens or the exact supplied values. Check quantities, not the
    # particular wording used to express negation, uncertainty or conditions.
    if Counter(VALUE.findall(wording)) != Counter(values.values()):
        raise WriterOutputError("Narrative changed, added or omitted a value")
    if "{{" in wording:
        raise WriterOutputError("Narrative contains an unknown token")
    return wording.strip()


def write_report(client, model, facts, config, response_path=None):
    plan = facts["report"]
    active = [section for section in plan["sections"] if section["include"]]
    expected = [
        {"title": s["title"], "block_ids": s["block_ids"], "paragraphs": []}
        for s in active
    ]
    supplied_plan = deepcopy(plan)
    for bid, block in supplied_plan["blocks"].items():
        block["text"], block["protected_values"] = protect_values(block["text"])
        block["can_rephrase"] = not block.get("locked", False)
        if block.get("locked"):
            block.pop("text")
            block.pop("protected_values")
    schema = {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "minItems": len(active),
                "maxItems": len(active),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "enum": [s["title"] for s in active],
                        },
                        "block_ids": {"type": "array", "items": {"type": "string"}},
                        "paragraphs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "block_id": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                                "required": ["block_id", "text"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["title", "block_ids", "paragraphs"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["sections"],
        "additionalProperties": False,
    }
    fallback = {"sections": expected}
    try:
        response = client.chat.completions.create(
            model=model,
            store=False,
            max_completion_tokens=4000,
            messages=[
                {
                    "role": "system",
                    "content": "\n".join(
                        config["global_instructions"] + config["writer_prompt"]
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"reconciled_report": supplied_plan}, ensure_ascii=False
                    ),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "draft_report",
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        output = response.choices[0].message.content or "" if response.choices else ""
        if response_path is not None:
            response_path.write_text(output, encoding="utf-8")
        if (
            not response.choices
            or response.choices[0].finish_reason != "stop"
            or response.choices[0].message.refusal
        ):
            raise WriterOutputError("Writer did not complete")
        result = json.loads(output)
        Draft202012Validator(schema).validate(result)
        return render_report(result, facts)
    except (ValueError, ValidationError, KeyError, TypeError, IndexError):
        # A bad arrangement must not discard valid facts or introduce new prose.
        return render_report(
            fallback,
            facts,
            "The writer arrangement could not be verified. This draft uses the reconciled wording and order.",
        )


def render_report(result, facts, notice=None):
    plan = facts["report"]
    active = [s for s in plan["sections"] if s["include"]]
    if [s["title"] for s in result["sections"]] != [s["title"] for s in active]:
        raise WriterOutputError("Writer changed the reconciled section order")
    rendered = []
    for supplied, expected in zip(result["sections"], active):
        if Counter(supplied["block_ids"]) != Counter(expected["block_ids"]):
            raise WriterOutputError(
                "Writer changed the reconciled facts or their section"
            )
        paragraphs = supplied.get("paragraphs", [])
        wording = {}
        local_fallback = False
        for paragraph in paragraphs:
            bid = paragraph["block_id"]
            if bid not in expected["block_ids"] or bid in wording:
                local_fallback = True
                continue
            # Locked content is rendered separately, regardless of model output.
            if not plan["blocks"][bid].get("locked"):
                wording[bid] = paragraph["text"]
        texts = []
        for bid in supplied["block_ids"]:
            block = plan["blocks"][bid]
            text = block["text"]
            if bid in wording:
                try:
                    text = restore_values(wording[bid], text)
                except WriterOutputError:
                    local_fallback = True
            if text not in texts:
                texts.append(text)
        if local_fallback:
            texts.append(
                "[REVIEW REQUIRED: Some wording in this section could not be verified; the affected paragraphs retain the reconciled wording.]"
            )
        # Mandatory wording is never delegated to the model.
        texts = (
            [t for t in expected["fixed_text"] if t != FCA_LINE]
            + texts
            + [t for t in expected["fixed_text"] if t == FCA_LINE]
        )
        texts += [
            "[REVIEW REQUIRED: " + note + "]"
            for note in expected.get("review_notes", [])
        ]
        rendered.append(dict(title=expected["title"], content="\n\n".join(texts)))
    if not rendered:
        raise WriterOutputError("Reconciliation provided no included report sections")
    notes = []
    if notice:
        notes.append(notice)
    rendered[0]["content"] = "**DRAFT — FOR HUMAN REVIEW**\n\n" + rendered[0]["content"]
    rendered[0]["content"] += "".join(
        "\n\n[REVIEW REQUIRED: " + text + "]" for text in dict.fromkeys(notes)
    )
    return format_document({"document_title": plan["title"]}, rendered)
