from pathlib import Path

import pytest

from agent_pipeline import ROOT
from agent_pipeline.extraction.sources import SourceTools


@pytest.mark.parametrize(
    "client",
    ["client_01_clean", "client_02_medium", "client_03_hard", "client_04_stretch"],
)
def test_unified_reader_routes_all_supplied_formats(client):
    tools = SourceTools(ROOT / "data" / client)
    inventory = tools.list_files()["sources"]
    by_name = {source["filename"]: source for source in inventory}
    for name in ("report_request.docx", "template_spec.md"):
        assert tools.read_source(by_name[name]["source_id"])["complete"]
    for source in inventory:
        result = tools.read_source(source["source_id"])
        assert result["ok"] and result["complete"], source["filename"]
        if source["format"] == "json":
            assert "data" in result and "raw_text" in result
        elif source["format"] == "docx":
            assert "blocks" in result
        elif source["format"] == "markdown":
            assert "text" in result
        else:
            assert result["image"]["image_url"].startswith("data:image/png;base64,")
    assert all(source["read_status"] == "read" for source in tools.sources.values())


def test_unified_reader_still_enforces_relevance_first():
    tools = SourceTools(ROOT / "data/client_01_clean")
    by_name = {source["filename"]: source for source in tools.list_files()["sources"]}
    sid = by_name["client_data_db.json"]["source_id"]
    assert not tools.read_source(sid)["ok"]
    assert tools.sources[sid]["read_status"] == "pending"


def test_unknown_id_is_rejected_without_path_access():
    tools = SourceTools(ROOT / "data/client_01_clean")
    assert not tools.read_source("../.env")["ok"]


def test_json_reader_preserves_missing_balance_without_location_metadata():
    tools = SourceTools(ROOT / "data/client_03_hard")
    by_name = {source["filename"]: source for source in tools.list_files()["sources"]}
    for name in ("report_request.docx", "template_spec.md"):
        tools.read_source(by_name[name]["source_id"])
    result = tools.read_source(by_name["client_data_db.json"]["source_id"])
    assert result["complete"]
    assert "locations" not in result
    assert result["data"]["holders"]["partner"]["accounts"][1]["value"] is None
