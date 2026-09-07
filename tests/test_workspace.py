import sqlite3
from collections.abc import Callable

import pytest
from conftest import SEARCH_FIXTURE

from mentor import db, query, workspace
from mentor.query import Filters

Seed = Callable[[dict | None], None]
NOTICES = SEARCH_FIXTURE["opportunitiesData"]
HRSA = NOTICES[0]["noticeId"]


@pytest.fixture
def seeded(conn: sqlite3.Connection, seed: Seed) -> sqlite3.Connection:
    seed()
    return conn


def events(conn: sqlite3.Connection, notice_id: str) -> list[tuple]:
    return [(e.field, e.old_value, e.new_value) for e in workspace.history(conn, notice_id)]


def test_track_inserts_with_events(seeded: sqlite3.Connection) -> None:
    tracked = workspace.track(seeded, HRSA)

    assert (tracked.notice_id, tracked.stage, tracked.pwin, tracked.notes) == (
        HRSA,
        "watching",
        None,
        None,
    )
    assert tracked.title == NOTICES[0]["title"] and tracked.agency == "HRSA HEADQUARTERS"
    assert events(seeded, HRSA) == [("stage", None, "watching")]


def test_track_updates_append_only_changed_fields(seeded: sqlite3.Connection) -> None:
    workspace.track(seeded, HRSA)
    workspace.track(seeded, HRSA, stage="pursuing", pwin=40)
    unchanged = workspace.track(seeded, HRSA, stage="pursuing", pwin=40)
    workspace.track(seeded, HRSA, pwin=55, notes="Incumbent is weak")

    assert (unchanged.stage, unchanged.pwin) == ("pursuing", 40)
    assert events(seeded, HRSA) == [
        ("stage", None, "watching"),
        ("stage", "watching", "pursuing"),
        ("pwin", None, "40"),
        ("pwin", "40", "55"),
        ("notes", None, "Incumbent is weak"),
    ]
    assert seeded.execute("SELECT count(*) FROM tracked_opportunities").fetchone() == (1,)


def test_track_unknown_notice_writes_nothing(seeded: sqlite3.Connection) -> None:
    with pytest.raises(workspace.NotFound):
        workspace.track(seeded, "nope")
    assert seeded.execute("SELECT count(*) FROM tracked_opportunity_events").fetchone() == (0,)
    with pytest.raises(workspace.NotFound):
        workspace.history(seeded, HRSA)


def test_pipeline_orders_by_stage_then_deadline(seeded: sqlite3.Connection) -> None:
    by_deadline = sorted(
        (n for n in NOTICES if n["responseDeadLine"]), key=lambda n: n["responseDeadLine"]
    )
    workspace.track(seeded, HRSA, stage="bid")  # undated
    workspace.track(seeded, by_deadline[-1]["noticeId"], stage="pursuing")
    workspace.track(seeded, by_deadline[0]["noticeId"], stage="pursuing")
    workspace.track(seeded, by_deadline[1]["noticeId"], stage="watching")

    order = [(t.stage, t.notice_id) for t in workspace.pipeline(seeded)]

    assert order == [
        ("watching", by_deadline[1]["noticeId"]),
        ("pursuing", by_deadline[0]["noticeId"]),
        ("pursuing", by_deadline[-1]["noticeId"]),
        ("bid", HRSA),
    ]


def test_other_users_rows_are_invisible(seeded: sqlite3.Connection) -> None:
    seeded.execute("INSERT INTO users (user_id, name) VALUES (2, 'other')")
    workspace.track(seeded, HRSA, user_id=2)
    workspace.save_search(seeded, "theirs", user_id=2)
    workspace.set_profile(seeded, user_id=2, name="Other LLC")

    assert workspace.pipeline(seeded) == []
    assert workspace.list_searches(seeded) == []
    assert workspace.get_profile(seeded) is None
    with pytest.raises(workspace.NotFound):
        workspace.history(seeded, HRSA)


def test_saved_search_round_trip_upsert_and_delete(seeded: sqlite3.Connection) -> None:
    first = workspace.save_search(
        seeded,
        "it",
        query_text="xylophone",
        filters=Filters(naics=("541512",), deadline_within_days=30),
    )
    again = workspace.save_search(seeded, "it", filters=Filters(set_asides=("SBA",)))

    assert first.filters == Filters(naics=("541512",), deadline_within_days=30)
    assert again.search_id == first.search_id and again.query is None
    assert again.filters == Filters(set_asides=("SBA",))
    assert [s.name for s in workspace.list_searches(seeded)] == ["it"]

    workspace.delete_search(seeded, "it")
    assert workspace.list_searches(seeded) == []
    with pytest.raises(workspace.NotFound):
        workspace.delete_search(seeded, "it")


def test_run_search_filters(seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-06T00:00:00Z")

    def run(name: str, **filters: object) -> list[str]:
        workspace.save_search(seeded, name, filters=Filters(**filters))
        return [hit.notice_id for hit in workspace.run_search(seeded, name)]

    assert len(run("all")) == 5
    assert len(run("sba", set_asides=("SBA",))) == 2
    assert len(run("army", agency_prefixes=("021.2100",))) == 2
    assert run("no-boundary", agency_prefixes=("021.21",)) == []
    assert run("naics", naics=("999999",)) == []
    soon = run("soon", deadline_within_days=5)
    assert len(soon) == 2 and all(
        n["responseDeadLine"] < "2026-09-11" for n in NOTICES if n["noticeId"] in soon
    )
    assert len(run("sba-army", set_asides=("SBA",), agency_prefixes=("021.2100",))) == 2
    assert run("sba-hhs", set_asides=("SBA",), agency_prefixes=("075",)) == []

    seeded.execute("UPDATE notices SET active = 0 WHERE notice_id = ?", (HRSA,))
    assert HRSA not in run("active")


def test_run_search_with_text_intersects_filters(seeded: sqlite3.Connection) -> None:
    seeded.execute("UPDATE notices SET description = 'quokka habitat'")
    workspace.save_search(seeded, "q", query_text="quokka", filters=Filters(set_asides=("SBA",)))

    hits = workspace.run_search(seeded, "q")

    assert len(hits) == 2 and all(h.source == "notice" for h in hits)
    assert len(query.search(seeded, "quokka")) == 5


def test_profile_upsert_merges_fields(conn: sqlite3.Connection) -> None:
    assert workspace.get_profile(conn) is None
    workspace.set_profile(conn, name="Example LLC", naics=("541512", "541511"))
    profile = workspace.set_profile(conn, certifications=("SB", "SDVOSB"), cage="1ABC2")

    assert profile.name == "Example LLC" and profile.cage == "1ABC2"
    assert profile.naics == ("541512", "541511")
    assert profile.certifications == ("SB", "SDVOSB")
    assert profile.target_agency_prefixes == () and profile.uei is None
