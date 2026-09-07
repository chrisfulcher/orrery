import sqlite3
from collections.abc import Callable

import pytest
from conftest import SEARCH_FIXTURE

from mentor import db, documents, query, workspace
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
    workspace.save_profile(seeded, '[company]\nname = "Other LLC"\n', user_id=2)

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


def test_profile_document_projects_into_the_profile_row(conn: sqlite3.Connection) -> None:
    assert workspace.get_profile(conn) is None
    assert workspace.profile_document(conn).startswith("# mentor company profile")
    body = (
        '[company]\nname = "Example LLC"\ncage = "1abc2"\n'
        '[offerings]\nnaics = ["541512", "541511"]\ncapability_statement = "We do IT."\n'
        '[markets]\nagency_prefixes = ["075"]\n'
        '[qualifications]\ncertifications = ["SB", "SDVOSB"]\n'
    )
    profile = workspace.save_profile(conn, body)
    assert profile.name == "Example LLC" and profile.cage == "1ABC2" and profile.uei is None
    assert profile.naics == ("541512", "541511") and profile.certifications == ("SB", "SDVOSB")
    assert (
        profile.target_agency_prefixes == ("075",) and profile.capability_statement == "We do IT."
    )
    assert profile.document is not None and profile.document["company"]["name"] == "Example LLC"
    assert workspace.profile_document(conn) == body  # verbatim, not re-rendered

    later = workspace.save_profile(conn, '[company]\nname = "Example LLC"\n')
    assert later.naics == () and later.certifications == ()  # the document is the whole truth
    assert [d.version for d in workspace.document_versions(conn, "profile")] == [1, 2]
    with pytest.raises(documents.DocumentError):
        workspace.save_profile(conn, "[company]\nnope = 1\n")
    assert len(workspace.document_versions(conn, "profile")) == 2


def test_profile_document_renders_a_legacy_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO company_profiles (user_id, name, uei, naics, certifications, updated_at)"
        " VALUES (1, 'Legacy LLC', 'UE9QJD4KK1L6', '[\"541512\"]', '[\"SB\"]', 'x')"
    )
    text = workspace.profile_document(conn)
    doc = documents.parse(text, documents.ProfileDocument)
    assert doc.company.name == "Legacy LLC" and doc.company.uei == "UE9QJD4KK1L6"
    assert doc.offerings.naics == ["541512"] and doc.qualifications.certifications == ["SB"]
    assert workspace.get_profile(conn).document is None  # nothing saved as a document yet


def test_profile_links_to_the_companys_entity_by_uei(
    conn: sqlite3.Connection, seed_awards: Callable[..., object]
) -> None:
    body = '[company]\nname = "Example LLC"\nuei = "UE9QJD4KK1L6"\n'
    assert workspace.save_profile(conn, body).entity_id is None
    seed_awards()
    profile = workspace.get_profile(conn)
    assert profile is not None and profile.entity_id is not None
    (uei,) = conn.execute(
        "SELECT uei FROM entities WHERE entity_id = ?", (profile.entity_id,)
    ).fetchone()
    assert uei == "UE9QJD4KK1L6"


def test_documents_are_versioned_per_user_and_append_only(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-07T00:00:00Z")
    assert workspace.latest_document(conn, "profile") is None
    first = workspace.save_document(conn, "profile", "[company]\nname = 'A'\n")
    second = workspace.save_document(conn, "profile", "[company]\nname = 'B'\n")
    assert (first.version, second.version) == (1, 2)
    assert workspace.latest_document(conn, "profile") == second
    assert [d.version for d in workspace.document_versions(conn, "profile")] == [1, 2]
    assert workspace.latest_document(conn, "workflow") is None
    conn.execute("INSERT INTO users (user_id, name) VALUES (2, 'other')")
    assert workspace.latest_document(conn, "profile", user_id=2) is None
    assert workspace.save_document(conn, "profile", "x = 1\n", user_id=2).version == 1
    with pytest.raises(ValueError):
        workspace.save_document(conn, "diary", "x = 1\n")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE workspace_documents SET body = 'z' WHERE document_id = ?", (first.document_id,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM workspace_documents WHERE document_id = ?", (first.document_id,))


def test_workflow_defaults_and_saves(conn: sqlite3.Connection) -> None:
    assert workspace.workflow(conn).keys()[0] == "identify"
    doc = workspace.save_workflow(
        conn,
        '[[stages]]\nkey = "find"\nname = "Find"\ngate = "Go"\n'
        '[[stages]]\nkey = "win"\nname = "Win"\n',
    )
    assert doc.keys() == ["find", "win"] and workspace.workflow(conn).keys() == ["find", "win"]
    with pytest.raises(documents.DocumentError):
        workspace.save_workflow(conn, "stages = []\n")
    assert len(workspace.document_versions(conn, "workflow")) == 1


def test_search_document_edit_replaces_the_search(seeded: sqlite3.Connection) -> None:
    workspace.save_search(seeded, "sba", filters=Filters(set_asides=("SBA",)))
    text = workspace.search_document(workspace.get_search(seeded, "sba"))
    assert 'set_asides = ["SBA"]' in text
    edited = workspace.save_search_document(
        seeded, "sba", 'query = "Microsoft"\nnaics = ["541512"]\ndeadline_within_days = 0\n'
    )
    assert edited.query == "Microsoft" and edited.filters == Filters(naics=("541512",))
