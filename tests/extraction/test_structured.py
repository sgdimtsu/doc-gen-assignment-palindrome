"""Structured evidence must survive extraction without model reproduction."""

from copy import deepcopy
from pathlib import Path
import json

import pytest

from agent_pipeline import ROOT
from agent_pipeline.extraction.sources import SourceTools
from agent_pipeline.extraction.results import ExtractionRecords
from agent_pipeline.validation.review import ExtractionValidator
from agent_pipeline.validation.checks import check_extraction


def imported(folder):
    sources = SourceTools(folder)
    inventory = sources.list_files()["sources"]
    for name in ["report_request.docx", "template_spec.md"]:
        sources.read_source(
            next(s["source_id"] for s in inventory if s["filename"] == name)
        )
    for source in inventory:
        if source["filename"] not in {"report_request.docx", "template_spec.md"}:
            sources.read_source(source["source_id"])
    candidate = dict(
        sources=[
            dict(source_id=s["source_id"], role="unknown", document_date=None)
            for s in inventory
        ],
        observations=[],
        extraction_issues=[],
    )
    structured = dict(
        protected=[o for rows in sources.protected.values() for o in rows],
        roles=sources.roles,
        request_fields=sources.request_fields,
    )
    store = ExtractionRecords(structured=structured)
    return sources, candidate, store


@pytest.mark.parametrize(
    "client",
    ["client_01_clean", "client_02_medium", "client_03_hard", "client_04_stretch"],
)
def test_all_supplied_databases_are_imported_exactly(client):
    folder = ROOT / "data" / client
    sources, candidate, store = imported(folder)
    result = store.assemble(candidate, sources.list_files()["sources"], "run")
    original = json.loads(
        (folder / "client_data_db.json").read_text(), parse_int=str, parse_float=str
    )
    accounts = [a for h in original["holders"].values() for a in h["accounts"]]
    valuations = [
        o for o in result["observations"] if o["observation_type"] == "valuation"
    ]
    assert len(valuations) == len(accounts)
    for raw, observation in zip(accounts, valuations):
        data = observation["data"]
        assert data["account"]["account_id"] == raw["account_id"]
        assert data["amount"] == raw["value"]
        assert data["valuation_date"] == raw["valuation_date"]
        assert data["currency"] == raw["currency"]
    evidence = ExtractionValidator()._load_evidence(folder, result)
    assert check_extraction(result, evidence, store.result_validator) == []


def test_model_cannot_replace_structured_values_or_misclassify_request_amount():
    sources, candidate, store = imported(ROOT / "data/client_01_clean")
    forged = deepcopy(store.structured["protected"][0])
    forged["data"]["risk_score"] = "99"
    candidate["observations"].append(forged)
    valuation = deepcopy(
        next(
            o
            for o in store.structured["protected"]
            if o["observation_type"] == "valuation"
        )
    )
    valuation["data"]["amount"] = "999999"
    candidate["observations"].append(valuation)
    result = store.assemble(candidate, sources.list_files()["sources"], "run")
    assert not any(
        o["data"].get("amount") == "999999" or o["data"].get("risk_score") == "99"
        for o in result["observations"]
    )
    assert (
        next(
            o
            for o in result["observations"]
            if o["data"].get("category") == "risk_profile"
        )["data"]["risk_score"]
        == "4"
    )


def test_decimal_precision_and_zero_and_null_are_distinct(tmp_path):
    folder = tmp_path
    (folder / "template_spec.md").write_text("## Example\nExample")
    from zipfile import ZipFile

    with ZipFile(folder / "report_request.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        )
    (folder / "unknown-name.json").write_text(
        '{"holders":{"client":{"name":"Alex","accounts":[{"account_id":"A","value":123456789.123456789,"currency":"GBP"},{"account_id":"B","value":0},{"account_id":"C","value":null}]}}}'
    )
    sources, candidate, store = imported(folder)
    result = store.assemble(candidate, sources.list_files()["sources"], "run")
    assert [
        o["data"]["amount"]
        for o in result["observations"]
        if o["observation_type"] == "valuation"
    ] == ["123456789.123456789", "0", None]


def test_unknown_json_layout_is_flagged_without_guessing_accounts(tmp_path):
    from zipfile import ZipFile

    (tmp_path / "template_spec.md").write_text("## Example\nExample")
    with ZipFile(tmp_path / "report_request.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        )
    (tmp_path / "unexpected.json").write_text('{"mystery_balance": 123}')
    sources, candidate, store = imported(tmp_path)
    result = store.assemble(candidate, sources.list_files()["sources"], "run")
    assert not result["observations"]
    assert any(i["issue_type"] == "schema_gap" for i in result["extraction_issues"])
