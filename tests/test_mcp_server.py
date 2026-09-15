import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SEARCH_FIXTURE
from mcp import Client

from orrery import mcp_server, workspace
from orrery.ingest.awards import AwardsResult

Seed = Callable[[dict | None], None]
SeedAwards = Callable[[list[dict] | None], AwardsResult]
HRSA = SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"]
EXPECTED_TOOLS = {
    "search", "notice", "entity", "upcoming", "pipeline", "track", "history", "saved_searches",
    "run_saved_search", "save_search", "queue_status", "quota_today", "profile", "awards",
    "contractor", "pursuits", "pursuit", "new_pursuit", "link_notice", "gate", "tasks", "task_done",
    "update_pursuit", "recompetes", "assessments",
}  # fmt: skip


@pytest.fixture
def store(
    conn: sqlite3.Connection, seed: Seed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> sqlite3.Connection:
    seed()
    monkeypatch.setenv("ORRERY_DATA_DIR", str(tmp_path))
    return conn


def test_tools_read_and_write_the_store(store: sqlite3.Connection) -> None:
    hits = mcp_server.search("Microsoft", set_aside=["SBA"])
    assert [hit.notice_id for hit in hits] and all(hit.source == "notice" for hit in hits)
    detail = mcp_server.notice(HRSA)
    assert detail.agency == "HRSA HEADQUARTERS" and len(detail.agency_chain) == 3
    assert detail.summary is None and detail.keywords == () and hits[0].work_type is None
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
        due = await client.call_tool("upcoming", {"days": 3650})
    assert not result.is_error
    assert result.structured_content["title"] == SEARCH_FIXTURE["opportunitiesData"][0]["title"]
    # The collapsed fields are on the wire, not just on the dataclass: the schema still
    # serialises after SearchHit gained them.
    assert not due.is_error
    first = due.structured_content["result"][0]
    assert first["notices"] == 1 and first["notice_type"] and first["solicitation_number"]


def test_award_tools(
    seed_awards: SeedAwards, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_awards()
    monkeypatch.setenv("ORRERY_DATA_DIR", str(tmp_path))
    rows = mcp_server.awards(office="75R602")
    assert [row.piid for row in rows] == ["75R60222F00009", "75R60224F00021"]
    assert mcp_server.awards(uei="PHZDZ8SJ5CM1")[0].vendor == "CDW GOVERNMENT LLC"
    assert [r.piid for r in mcp_server.recompetes(months=18)] == [
        "75R60222F00009", "75R60224F00021",
    ]  # fmt: skip
    detail = mcp_server.contractor("UE9QJD4KK1L6")
    assert detail.kind == "contractor" and detail.awards_count == 1
    assert mcp_server.notice(HRSA).incumbent is not None
    with pytest.raises(workspace.NotFound):
        mcp_server.contractor("NOPE")


async def test_pursuit_tools(
    seed_awards: SeedAwards, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_awards()
    monkeypatch.setenv("ORRERY_DATA_DIR", str(tmp_path))
    opened = mcp_server.new_pursuit("Help desk recompete", office_code="75R602", naics="541512")
    assert (opened.stage, opened.office, opened.open_tasks) == ("identify", "HRSA HEADQUARTERS", 3)
    linked = mcp_server.link_notice(opened.pursuit_id, HRSA)
    assert linked.notice_id == HRSA
    detail = mcp_server.pursuit(opened.pursuit_id)
    assert [t.task_id for t in mcp_server.tasks()] == [t.task_id for t in detail.tasks]
    assert mcp_server.tasks(subject_type="entity") == []
    mcp_server.task_done(detail.tasks[0].task_id)
    assert len(mcp_server.tasks()) == 2
    assert len(mcp_server.tasks(open_only=False)) == 3
    assert [t.subject_type for t in mcp_server.tasks(open_only=False)] == ["pursuit"] * 3
    assert mcp_server.update_pursuit(opened.pursuit_id, pwin=35).pwin == 35
    assert mcp_server.gate(opened.pursuit_id, "go", "fits").stage == "qualify"
    assert [p.pursuit_id for p in mcp_server.pursuits(stage="qualify")] == [opened.pursuit_id]
    with pytest.raises(ValueError):
        mcp_server.gate(opened.pursuit_id, "maybe", "x")
    async with Client(mcp_server.server) as client:
        result = await client.call_tool("pursuit", {"pursuit_id": opened.pursuit_id})
    assert not result.is_error and result.structured_content["pursuit"]["stage"] == "qualify"


def test_assessments_are_read_only(
    seed_awards: SeedAwards, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_awards()
    monkeypatch.setenv("ORRERY_DATA_DIR", str(tmp_path))
    opened = mcp_server.new_pursuit("X")
    assert mcp_server.assessments(opened.pursuit_id) == []
    with mcp_server._conn() as conn:
        workspace.save_assessment(
            conn, opened.pursuit_id, slot="fast", provider="openai", model="m", prompt_version=1,
            profile_version=None, inputs_hash="h", input_tokens=1, output_tokens=1,
            raw_response="{}", result={"fit": 10},
        )  # fmt: skip
    assert [a.result["fit"] for a in mcp_server.assessments(opened.pursuit_id)] == [10]
    with pytest.raises(workspace.NotFound):
        mcp_server.assessments(999)


def test_contractor_and_entity_carry_exclusions(
    conn: sqlite3.Connection,
    settings,
    seed_awards: SeedAwards,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No new tool: an agent asking about a vendor is already asking the right question."""
    from conftest import write_exclusions
    from test_ingest_exclusions import DAY_257, FIRM_ONE, SAM_ONE, VENDORS

    from orrery.ingest.exclusions import ingest_extract

    seed_awards(VENDORS)
    ingest_extract(conn, settings, write_exclusions(tmp_path, [FIRM_ONE], DAY_257))
    monkeypatch.setenv("ORRERY_DATA_DIR", str(tmp_path))

    detail = mcp_server.contractor("EXCL00000001")

    assert detail.excluded is True
    assert [e.sam_number for e in detail.exclusions] == [SAM_ONE]
    assert mcp_server.entity(detail.entity_id).excluded is True
