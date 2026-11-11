"""One writer call; report structure comes from the client's Markdown specification."""

import json
import re

from jsonschema import Draft202012Validator, ValidationError

from document_formatter.formatting import format_document

# Existing fixed wording from the supplied baseline template. The client spec
# refers to this sentence without reproducing it.
FCA_LINE = "This firm is authorised and regulated by the Financial Conduct Authority."


class WriterOutputError(ValueError):
    """A safe, code-generated explanation of an invalid writer response."""


def specification_sections(specification):
    headings = list(re.finditer(r"^##[ \t]+(.+)$", specification, re.MULTILINE))
    sections = []
    for index, heading in enumerate(headings):
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(specification)
        )
        sections.append(
            {
                "title": heading[1].strip(),
                "requirements": specification[heading.end() : end].strip(),
            }
        )
    if not sections or len({s["title"] for s in sections}) != len(sections):
        raise WriterOutputError(
            "Client specification requires distinct level-two section headings"
        )
    return sections


def _fixed_wording(requirements):
    fixed = [
        " ".join(match[1].split())
        for match in re.finditer(
            r'word for word:\s*"([^\"]+)"', requirements, re.IGNORECASE
        )
    ]
    if re.search(
        r"FCA.*authori[sz]ation|authori[sz]ation.*FCA", requirements, re.IGNORECASE
    ) and not any(
        "FCA" in text or "Financial Conduct Authority" in text for text in fixed
    ):
        fixed.append(FCA_LINE)
    return fixed


def write_report(
    client, model, specification, report_request, facts, config, response_path=None
):
    sections = specification_sections(specification)
    issues = facts.get("issues", [])
    schema = {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "minItems": len(sections),
                "maxItems": len(sections),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "enum": [s["title"] for s in sections],
                        },
                        "include": {"type": "boolean"},
                        "content": {"type": "string"},
                        "review_notes": {
                            "type": "array",
                            "maxItems": len(issues),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "issue_index": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": max(0, len(issues) - 1),
                                    },
                                    "text": {"type": "string"},
                                },
                                "required": ["issue_index", "text"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["title", "include", "content", "review_notes"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["sections"],
        "additionalProperties": False,
    }
    rules = "\n".join(config["global_instructions"] + config["writer_prompt"])
    rules += (
        "\nReturn one entry for every supplied section in the exact order. Set include=false "
        "only when the specification makes that section conditional and its condition is not met. "
        "Do not put report headings in content. For each issue in reconciled_facts.issues, put its "
        "zero-based issue_index and a plain-language review note in the most relevant included section. "
        "Account for every issue using review_notes only, including uncertainty about scope, proposed accounts "
        "and validation quality. Translate technical concerns into a need to verify the affected "
        "information; do not expose internal IDs or file paths. Do not put notes in omitted sections."
    )
    response = client.chat.completions.create(
        model=model,
        store=False,
        max_completion_tokens=10000,
        messages=[
            {"role": "system", "content": rules},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "template_spec": specification,
                        "report_request": report_request,
                        "sections": sections,
                        "reconciled_facts": facts,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "draft_report", "strict": True, "schema": schema},
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
        reason = response.choices[0].finish_reason if response.choices else "no choices"
        raise WriterOutputError("Writer response did not complete (" + reason + ")")
    try:
        result = json.loads(output)
        Draft202012Validator(schema).validate(result)
    except (ValueError, ValidationError):
        raise WriterOutputError(
            "Writer returned invalid section JSON; inspect writer_response.json"
        ) from None
    return render_report(result, specification, facts)


def _fallback_note(issue):
    kind = issue["issue_type"]
    if kind == "unresolved_account_match":
        return "An account reference in the supporting information could not be matched uniquely. Confirm the account details before proceeding."
    if kind == "interpretation_required":
        return "Some supporting instructions or circumstances need adviser interpretation before this draft is finalised."
    if kind in {
        "validation_stale",
        "validation_failed",
        "validation_incomplete",
        "validation_not_reviewed",
    }:
        return "The supporting information and this draft require human verification; automated review has not confirmed the final version."
    # Retain the substantive concern, stripping internal observation identifiers.
    return re.sub(
        r"\b[0-9a-f]{32}:observation-\d+\b",
        "a supporting observation",
        issue["message"],
    )


def _clean_text(text):
    text = re.sub(r"\[(?:Issue|Issues)\s+\d+(?:[\s,–-]+\d+)*\]", "", text, flags=re.I)
    text = re.sub(r"\b[0-9a-f]{32}:observation-\d+\b", "a supporting observation", text)
    return text.strip()


def _note_text(text):
    text = _clean_text(text)
    # Accept legacy/model-generated wrappers, but render the marker only once.
    text = re.sub(r"^(?:\[?REVIEW REQUIRED\s*:\s*)+", "", text, flags=re.I)
    return text.rstrip("]").strip()


def render_report(result, specification, facts):
    sections = specification_sections(specification)
    issues = facts.get("issues", [])
    if [s["title"] for s in result["sections"]] != [s["title"] for s in sections]:
        raise WriterOutputError("Writer changed the specified section order")
    rendered, covered, notes_seen = [], set(), set()

    def append_note(content, text):
        text = _note_text(text)
        key = " ".join(text.casefold().split())
        if not text or key in notes_seen:
            return content
        notes_seen.add(key)
        return content + "\n\n[REVIEW REQUIRED: " + text + "]"

    for section, definition in zip(result["sections"], sections):
        fixed = _fixed_wording(definition["requirements"])
        if not section["include"]:
            if fixed:
                raise WriterOutputError("An omitted section contains required wording")
            continue
        content = section["content"].strip()
        if not content or re.search(r"^#{1,2}\s", content, re.MULTILINE):
            raise WriterOutputError(
                "Writer must return section content without report headings"
            )
        # Review prose belongs to the structured notes, with issue fallbacks below.
        content = re.sub(r"\[REVIEW REQUIRED:.*?\]", "", content, flags=re.I | re.S)
        content = "\n\n".join(
            paragraph
            for paragraph in content.split("\n\n")
            if not re.match(r"^REVIEW REQUIRED\s*:", paragraph.strip(), re.I)
        )
        content = _clean_text(content)
        if not content:
            content = "The information for this section requires confirmation."
        for text in fixed:
            if text not in content:
                content = (
                    content + "\n\n" + text
                    if text == FCA_LINE
                    else text + "\n\n" + content
                )
        for note in section["review_notes"]:
            if not note["text"].strip() or note["issue_index"] in covered:
                continue
            covered.add(note["issue_index"])
            content = append_note(content, note["text"])
        rendered.append({"title": section["title"], "content": content})
    if not rendered:
        raise WriterOutputError("Writer returned no report sections")
    for index in sorted(set(range(len(issues))) - covered):
        rendered[0]["content"] = append_note(
            rendered[0]["content"], _fallback_note(issues[index])
        )
    processing_notice = any(
        issue["issue_type"]
        in {
            "validation_stale",
            "validation_failed",
            "validation_incomplete",
            "validation_not_reviewed",
        }
        for issue in issues
    )
    if (
        facts.get("validation_state", "not_reviewed") != "passed"
        and not processing_notice
    ):
        rendered[0]["content"] = append_note(
            rendered[0]["content"],
            "Automated review has not confirmed the final extraction. Verify this draft against the source information before proceeding.",
        )
    title_match = re.search(r"^#[ \t]+(.+)$", specification, re.MULTILINE)
    title = (
        title_match[1].removeprefix("Report specification: ").strip()
        if title_match
        else "Advice Report"
    )
    # A draft label is metadata in the first section, not an extra report section.
    rendered[0]["content"] = "**DRAFT — FOR HUMAN REVIEW**\n\n" + rendered[0]["content"]
    return format_document({"document_title": title}, rendered)
