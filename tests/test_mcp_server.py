import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SEARCH_FIXTURE
from mcp import Client

from mentor import mcp_server, workspace
from mentor.ingest.awards import AwardsResult

Seed = Callable[[dict | None], None]
SeedAwards = Callable[[list[dict] | None], AwardsResult]
HRSA = SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"]
EXPECTED_TOOLS = {
    "search", "notice", "entity", "upcoming", "pipeline", "track", "history", "saved_searches",
    "run_saved_search", "save_search", "queue_status", "quota_today", "profile", "awards",
    "contractor",
}  # fmt: skip


@pytest.fixture
def store(
    conn: sqlite3.Connection, seed: Seed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> sqlite3.Connection:
    seed()
    monkeypatch.setenv("MENTOR_DATA_DIR", str(tmp_path))
    return conn


def test_tools_read_and_write_the_store(store: sqlite3.Connection) -> None:
    hits = mcp_server.search("Microsoft", set_aside=["SBA"])
    assert [hit.notice_id for hit in hits] and all(hit.source == "notice" for hit in hits)
    detail = mcp_server.notice(HRSA)
    assert detail.agency == "HRSA HEADQUARTERS" and len(detail.agency_chain) == 3
    assert mcp_server.entity(detail.agency_chain[0].entity_id).children
    assert len(mcp_server.upcoming(days=3650)) == 4

    tracked = mcp_server.track(HRSA, stage=workspace.Stage.PURSUING, pwin=40)
    assert tracked.stage == "qualify" and [t.notice_id for t in mcp_server.pipeline()] == [HRSA]
    assert [e.field for e in mcp_server.history(HRSA)] == [
        "stage",
        "notice",
        "gate",
        "stage",
        "pwin",
    ]

    saved = mcp_server.save_search("sba", set_aside=["SBA"])
    assert saved.filters.set_asides == ("SBA",)
    assert len(mcp_server.run_saved_search("sba")) == 2
    assert [s.name for s in mcp_server.saved_searches()] == ["sba"]

    status = mcp_server.queue_status()
    assert (status.descriptions_pending, status.attachments_pending) == (5, 20)
    budget = mcp_server.quota_today()
    assert budget["spent"] == 1 and budget["remaining"] == budget["budget"] - 1
    assert mcp_server.profile() is None
    with store:
        workspace.save_profile(
            store, '[company]\nname = "Example LLC"\n[offerings]\nnaics = ["541512"]\n'
        )
    profile = mcp_server.profile()
    assert profile is not None and profile.document == {
        **profile.document,
        "company": {"name": "Example LLC", "uei": None, "cage": None},
    }


def test_unknown_ids_raise(store: sqlite3.Connection) -> None:
    with pytest.raises(workspace.NotFound):
        mcp_server.notice("nope")
    with pytest.raises(workspace.NotFound):
        mcp_server.entity(999)


async def test_tools_are_listed_over_the_protocol(store: sqlite3.Connection) -> None:
    async with Client(mcp_server.server) as client:
        listed = await client.list_tools()
        tools = getattr(listed, "tools", listed)
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
        result = await client.call_tool("notice", {"notice_id": HRSA})
    assert not result.is_error
    assert result.structured_content["title"] == SEARCH_FIXTURE["opportunitiesData"][0]["title"]


def test_award_tools(
    seed_awards: SeedAwards, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_awards()
    monkeypatch.setenv("MENTOR_DATA_DIR", str(tmp_path))
    rows = mcp_server.awards(office="75R602")
    assert [row.piid for row in rows] == ["75R60222F00009", "75R60224F00021"]
    assert mcp_server.awards(uei="PHZDZ8SJ5CM1")[0].vendor == "CDW GOVERNMENT LLC"
    detail = mcp_server.contractor("UE9QJD4KK1L6")
    assert detail.kind == "contractor" and detail.awards_count == 1
    assert mcp_server.notice(HRSA).incumbent is not None
    with pytest.raises(workspace.NotFound):
        mcp_server.contractor("NOPE")
