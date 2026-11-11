"""Evaluate a saved report: fixed checks plus one independent, advisory LLM judge."""

import argparse
import json
import os
from pathlib import Path
import re

from jsonschema import Draft202012Validator
from openai import OpenAI

from agent_pipeline import ROOT
from agent_pipeline.cli import client_directory, load_environment
from agent_pipeline.extraction.sources import SourceTools

GROUND = (
    "factual_correctness",
    "completeness",
    "uncertainty_handling",
    "report_requirements",
)
QUALITY = ("readability_clarity", "conciseness_relevance", "tone_presentation")
RATINGS = ("meets", "partly_meets", "does_not_meet", "unable_to_assess")
LIMITATION = "Automated prototype evaluation only; no human review or human calibration was performed. A pass is not approval of financial advice."


def normalise(text):
    return " ".join(text.split())


def code_checks(report, specification, expected):
    checks = []

    def add(name, passed, severity="requirement"):
        checks.append(dict(check=name, passed=passed, severity=severity))

    matches = list(re.finditer(r"^##[ \t]+(.+)$", specification, re.M))
    titles = []
    for index, match in enumerate(matches):
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(specification)
        )
        requirements = specification[match.end() : end]
        conditional = re.search(r"\bONLY if\b", requirements, re.I)
        if conditional and re.search(r"sell|dispos|capital gain", requirements, re.I):
            if expected["tax_section_required"]:
                titles.append(match[1].strip())
        elif conditional:
            add("Unsupported conditional requirement: " + match[1].strip(), None)
        else:
            titles.append(match[1].strip())
    add(
        "Required sections and order",
        bool(titles) and re.findall(r"^##[ \t]+(.+)$", report, re.M) == titles,
    )
    required = [
        normalise(m[1])
        for m in re.finditer(r'word for word:\s*"([^\"]+)"', specification, re.I)
    ]
    if re.search(r"FCA.*authori[sz]ation|authori[sz]ation.*FCA", specification, re.I):
        required.append(
            "This firm is authorised and regulated by the Financial Conduct Authority."
        )
    for text in dict.fromkeys(required):
        add("Mandatory wording: " + text, normalise(report).count(text) == 1)
    columns = re.search(r"columns\s+([^\n]+\|[^\n]+)", specification, re.I)
    if columns:
        wanted = [v.strip().rstrip(".").casefold() for v in columns[1].split("|")]
        headers = [
            [v.strip().casefold() for v in line.strip().strip("|").split("|")]
            for line in report.splitlines()
            if line.strip().startswith("|")
        ]
        add("Required table columns", wanted in headers)
    add(
        "No leaked internal references",
        not re.search(
            r"\b(?:source-\d+|block-\d+|[0-9a-f]{32}:observation-\d+)\b|\[Issues?\s+\d",
            report,
            re.I,
        ),
        "presentation",
    )
    add(
        "No duplicated review markers",
        not re.search(r"REVIEW REQUIRED:\s*REVIEW REQUIRED:", report, re.I),
        "presentation",
    )
    return checks


def load_sources(client_dir):
    reader = SourceTools(client_dir)
    inventory = reader.list_files()
    if inventory["missing_required_files"]:
        raise ValueError("Required report request or specification is missing")
    files = inventory["sources"]
    ordered = sorted(
        files,
        key=lambda s: s["filename"] not in {"report_request.docx", "template_spec.md"},
    )
    sources, errors = [], []
    for source in ordered:
        result = reader.read_source(source["source_id"])
        text = result.get("raw_text", result.get("text", ""))
        if "blocks" in result:
            text = "\n".join(
                b.get("text", "\t".join(b.get("cells", []))) for b in result["blocks"]
            )
        limitations = result.get("limitations", [])
        if not result.get("ok") or not result.get("complete"):
            errors.append("Incomplete source reading: " + source["filename"])
        sources.append(
            dict(
                filename=source["filename"],
                text=text,
                image=result.get("image"),
                limitations=limitations,
            )
        )
    return sources, errors


def judge_schema(filenames, expected_outcomes):
    def obj(properties):
        return dict(
            type="object",
            properties=properties,
            required=list(properties),
            additionalProperties=False,
        )

    string = {"type": "string"}
    assessment = obj(
        {
            "rating": {"type": "string", "enum": list(RATINGS)},
            "explanation": string,
            "report_example": string,
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": filenames},
            },
        }
    )
    finding = obj(
        {
            "criterion": {"type": "string", "enum": list(GROUND + QUALITY)},
            "severity": {
                "type": "string",
                "enum": ["critical", "requirement", "presentation", "unverified"],
            },
            "explanation": string,
            "report_example": string,
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": filenames},
            },
        }
    )
    return obj(
        {
            "outcome_checks": obj({key: assessment for key in expected_outcomes}),
            "criteria": obj({name: assessment for name in GROUND + QUALITY}),
            "findings": {"type": "array", "items": finding},
        }
    )


def overall_outcome(checks, judgment, errors):
    if any(c["passed"] is False and c["severity"] == "requirement" for c in checks):
        return "fail"
    if errors or not judgment:
        return "needs_review"
    if any(c["rating"] == "does_not_meet" for c in judgment["outcome_checks"].values()):
        return "fail"
    if any(judgment["criteria"][name]["rating"] == "does_not_meet" for name in GROUND):
        return "fail"
    if any(
        f["criterion"] in GROUND and f["severity"] in {"critical", "requirement"}
        for f in judgment["findings"]
    ):
        return "fail"
    ratings = list(judgment["outcome_checks"].values()) + list(
        judgment["criteria"].values()
    )
    if (
        any(c["passed"] is not True for c in checks)
        or judgment["findings"]
        or any(c["rating"] != "meets" for c in ratings)
    ):
        return "needs_review"
    return "pass"


def judge_report(client, model, report, sources, expected, prompt):
    outcomes = {
        f"expected_{i + 1}": text
        for i, text in enumerate(expected["expected_outcomes"])
    }
    schema = judge_schema([s["filename"] for s in sources], outcomes)
    content = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "report": report,
                    "expected_outcomes": outcomes,
                    "sources": [
                        {k: v for k, v in s.items() if k != "image"} for s in sources
                    ],
                },
                ensure_ascii=False,
            ),
        }
    ]
    for source in sources:
        if source["image"]:
            content += [
                {"type": "text", "text": "Source image: " + source["filename"]},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": source["image"]["image_url"],
                        "detail": "high",
                    },
                },
            ]
    if len(json.dumps(content).encode()) > 4_000_000:
        raise ValueError("Evaluation evidence exceeds the prototype input limit")
    response = client.chat.completions.create(
        model=model,
        store=False,
        temperature=0,
        max_completion_tokens=8000,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "report_evaluation",
                "strict": True,
                "schema": schema,
            },
        },
    )
    if (
        not response.choices
        or response.choices[0].finish_reason != "stop"
        or getattr(response.choices[0].message, "refusal", None)
    ):
        raise ValueError("Evaluator did not complete")
    result = json.loads(response.choices[0].message.content)
    Draft202012Validator(schema).validate(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--expectations",
        type=Path,
        default=Path(__file__).with_name("expectations.json"),
    )
    parser.add_argument("--model", help="Overrides EVALUATION_MODEL; default gpt-4.1")
    args = parser.parse_args(argv)
    client_dir = client_directory(parser, args.client, args.data_dir)
    try:
        summary = json.loads((args.run_dir / "summary.json").read_text())
        if summary["client"] != args.client:
            raise ValueError("Run belongs to a different client")
        report = (args.run_dir / "report.md").read_text()
        specification = (client_dir / "template_spec.md").read_text()
        expected = json.loads(args.expectations.read_text())[args.client]
        if (
            not isinstance(expected["tax_section_required"], bool)
            or not expected["expected_outcomes"]
        ):
            raise ValueError("Expected outcomes are missing or invalid")
        sources, errors = load_sources(client_dir)
        prompt = (Path(__file__).with_name("judge_prompt.md")).read_text()
    except (OSError, ValueError, KeyError) as error:
        parser.error("Cannot load evaluation inputs: " + str(error))
    checks = code_checks(report, specification, expected)
    load_environment()
    model = args.model or os.environ.get("EVALUATION_MODEL") or "gpt-4.1"
    judgment = None
    if not errors:
        try:
            with OpenAI(timeout=90, max_retries=1) as client:
                judgment = judge_report(
                    client, model, report, sources, expected, prompt
                )
        except Exception as error:
            # SDK errors can contain credentials or source content; save only type.
            errors.append("Evaluator failed: " + type(error).__name__)
    outcome = overall_outcome(checks, judgment, errors)
    result = dict(
        client=args.client,
        outcome=outcome,
        model=model,
        code_checks=checks,
        expected_outcomes=expected["expected_outcomes"],
        judgment=judgment,
        evaluation_errors=errors,
        limitation=LIMITATION,
    )
    path = args.run_dir / "evaluation.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print("Evaluation: " + outcome)
    print("Results: " + str(path))
    for error in errors:
        print(error)
    return {"pass": 0, "needs_review": 1, "fail": 2}[outcome]


if __name__ == "__main__":
    raise SystemExit(main())
