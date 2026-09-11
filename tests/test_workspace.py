import sqlite3
from collections.abc import Callable

import pytest
from conftest import SEARCH_FIXTURE

from orrery import db, documents, query, workspace
from orrery.query import Filters

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
        "identify",
        None,
        None,
    )
    assert tracked.title == NOTICES[0]["title"] and tracked.agency == "HRSA HEADQUARTERS"
    assert events(seeded, HRSA) == [("stage", None, "identify"), ("notice", None, HRSA)]


def test_track_advances_through_recorded_gates(seeded: sqlite3.Connection) -> None:
    workspace.track(seeded, HRSA)
    workspace.track(seeded, HRSA, stage="pursuing", pwin=40)
    unchanged = workspace.track(seeded, HRSA, stage="pursuing", pwin=40)
    workspace.track(seeded, HRSA, pwin=55, notes="Incumbent is weak")

    assert (unchanged.stage, unchanged.pwin) == ("qualify", 40)
    assert events(seeded, HRSA) == [
        ("stage", None, "identify"),
        ("notice", None, HRSA),
        ("gate", "Pursuit Gate", "go"),
        ("stage", "identify", "qualify"),
        ("pwin", None, "40"),
        ("pwin", "40", "55"),
        ("notes", None, "Incumbent is weak"),
    ]
    assert seeded.execute("SELECT count(*) FROM pursuits").fetchone() == (1,)
    with pytest.raises(ValueError, match="earlier"):
        workspace.track(seeded, HRSA, stage="watching")
    assert workspace.track(seeded, HRSA, stage="proposal").stage == "proposal"
    assert workspace.track(seeded, HRSA, stage="won").stage == "post-award"
    assert workspace.pursuit_for_notice(seeded, HRSA).outcome == "won"


def test_track_unknown_notice_writes_nothing(seeded: sqlite3.Connection) -> None:
    with pytest.raises(workspace.NotFound):
        workspace.track(seeded, "nope")
    assert seeded.execute("SELECT count(*) FROM pursuit_events").fetchone() == (0,)
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
        ("identify", by_deadline[1]["noticeId"]),
        ("qualify", by_deadline[0]["noticeId"]),
        ("qualify", by_deadline[-1]["noticeId"]),
        ("proposal", HRSA),
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
    assert workspace.profile_document(conn).startswith("# orrery company profile")
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


def pursuit_events(conn: sqlite3.Connection, pursuit_id: int) -> list[tuple]:
    return [
        (e.field, e.old_value, e.new_value, e.note)
        for e in workspace.pursuit(conn, pursuit_id).events
    ]


def test_pursuit_lifecycle_records_every_decision(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-07T12:00:00Z")
    p = workspace.new_pursuit(conn, "Help desk recompete", summary="HRSA needs a help desk")
    assert (p.stage, p.open, p.open_tasks) == ("identify", True, 3)
    detail = workspace.pursuit(conn, p.pursuit_id)
    assert detail.gate == "Pursuit Gate" and detail.gate_ready is False

    held = workspace.gate(conn, p.pursuit_id, "hold", "budget unclear", until="2026-10-01")
    assert held.held_until == "2026-10-01"
    for task in detail.tasks:
        workspace.complete_task(conn, task.task_id)
    assert workspace.pursuit(conn, p.pursuit_id).gate_ready is False  # still held
    gone = workspace.gate(conn, p.pursuit_id, "go", "fits the profile")
    assert (gone.stage, gone.held_until, gone.open_tasks) == ("qualify", None, 4)
    task = workspace.add_task(
        conn, "Call the COR", subject_type="pursuit", subject_id=p.pursuit_id, due="2026-09-10"
    )
    assert (task.origin, task.stage, task.due) == ("user", "qualify", "2026-09-10")

    closed = workspace.gate(conn, p.pursuit_id, "no-go", "incumbent is entrenched")
    assert (closed.outcome, closed.open) == ("no-bid", False)
    with pytest.raises(ValueError, match="closed"):
        workspace.gate(conn, p.pursuit_id, "go", "x")
    reopened = workspace.reopen(conn, p.pursuit_id, "incumbent lost their key staff")
    assert (reopened.outcome, reopened.open, reopened.stage) == (None, True, "qualify")
    back = workspace.move_back(conn, p.pursuit_id, "identify", "re-check the requirement")
    assert back.stage == "identify" and back.open_tasks == 5  # nothing re-seeded
    with pytest.raises(ValueError, match="earlier"):
        workspace.move_back(conn, p.pursuit_id, "capture", "x")
    won = workspace.set_outcome(conn, p.pursuit_id, "won", "award received")
    assert (won.stage, won.outcome, won.open) == ("post-award", "won", True)
    assert workspace.pursuit(conn, p.pursuit_id).gate is None
    done = workspace.close_pursuit(conn, p.pursuit_id)
    assert done.open is False and done.outcome == "won"
    assert (
        workspace.pursuits(conn) == [] and len(workspace.pursuits(conn, include_closed=True)) == 1
    )

    fields = [e[0] for e in pursuit_events(conn, p.pursuit_id)]
    assert fields == [
        "stage", "gate", "hold", "task", "task", "task", "gate", "stage", "task", "gate",
        "outcome", "closed", "reopened", "stage", "outcome", "stage", "closed",
    ]  # fmt: skip
    assert ("gate", "Pursuit Gate", "hold", "budget unclear") in pursuit_events(conn, p.pursuit_id)
    assert ("stage", "qualify", "identify", "re-check the requirement") in pursuit_events(
        conn, p.pursuit_id
    )


def test_gate_and_outcome_rules(conn: sqlite3.Connection) -> None:
    p = workspace.new_pursuit(conn, "X")
    with pytest.raises(ValueError, match="rationale"):
        workspace.gate(conn, p.pursuit_id, "go", "  ")
    with pytest.raises(ValueError, match="revisit date"):
        workspace.gate(conn, p.pursuit_id, "hold", "later")
    with pytest.raises(ValueError, match="go, no-go, or hold"):
        workspace.gate(conn, p.pursuit_id, "maybe", "x")
    with pytest.raises(ValueError, match="is open"):
        workspace.reopen(conn, p.pursuit_id, "x")
    with pytest.raises(workspace.NotFound):
        workspace.pursuit(conn, 99)
    with pytest.raises(ValueError, match="title"):
        workspace.new_pursuit(conn, " ")
    won = workspace.set_outcome(conn, p.pursuit_id, "won", "direct award")
    with pytest.raises(ValueError, match="no gate"):
        workspace.gate(conn, won.pursuit_id, "go", "x")
    with pytest.raises(ValueError, match="cannot be removed"):
        workspace.save_workflow(conn, '[[stages]]\nkey = "find"\nname = "Find"\n')


def test_new_pursuit_from_a_notice_and_a_contract(
    conn: sqlite3.Connection, seed_awards: Callable[..., object]
) -> None:
    seed_awards()
    p = workspace.new_pursuit(conn, "From the notice", notice_id=HRSA)
    assert (p.office_code, p.naics_code, p.office, p.notices) == (
        "75R602", "541512", "HRSA HEADQUARTERS", 1,
    )  # fmt: skip
    (contract_id,) = conn.execute(
        "SELECT contract_id FROM contracts WHERE piid = '75R60222F00009'"
    ).fetchone()
    q = workspace.new_pursuit(conn, "From the award", contract_id=contract_id)
    detail = workspace.pursuit(conn, q.pursuit_id)
    assert (q.office_code, q.naics_code, q.incumbent) == ("75R602", "541512", "LEIDOS, INC.")
    assert detail.incumbent is not None and detail.incumbent.piid == "75R60222F00009"
    assert [n.notice_id for n in detail.related_notices] == [HRSA]  # the prior solicitation
    assert [(d.kind, d.date) for d in detail.dates] == [
        ("pop_end", "2026-02-28"),
        ("pop_potential_end", "2027-02-28"),
    ]
    role = workspace.NOTICE_ROLES.get(NOTICES[0]["type"], "other")
    linked = workspace.link_notice(conn, q.pursuit_id, HRSA)
    assert linked.role == role
    assert workspace.link_notice(conn, q.pursuit_id, HRSA).role == role
    assert workspace.pursuit_for_notice(conn, HRSA).pursuit_id == q.pursuit_id  # latest open


def test_dashboard_classifies_the_weeks_work(
    conn: sqlite3.Connection, seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-08-01T00:00:00Z")
    stale = workspace.new_pursuit(conn, "Stale")
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-01T00:00:00Z")
    due = workspace.new_pursuit(conn, "Due soon")
    workspace.add_task(
        conn, "Call", subject_type="pursuit", subject_id=due.pursuit_id, due="2026-09-03"
    )
    workspace.add_task(
        conn, "Late", subject_type="pursuit", subject_id=due.pursuit_id, due="2026-08-30"
    )
    workspace.add_task(
        conn, "Far", subject_type="pursuit", subject_id=due.pursuit_id, due="2026-10-30"
    )
    ready = workspace.new_pursuit(conn, "Ready")
    for task in workspace.pursuit(conn, ready.pursuit_id).tasks:
        workspace.complete_task(conn, task.task_id)
    held = workspace.new_pursuit(conn, "Held")
    workspace.gate(conn, held.pursuit_id, "hold", "wait", until="2026-09-05")
    dated = next(n for n in NOTICES if n["responseDeadLine"])
    noticed = workspace.new_pursuit(conn, "With a notice", notice_id=dated["noticeId"])
    (deadline,) = conn.execute(
        "SELECT substr(response_deadline, 1, 10) FROM notices WHERE notice_id = ?",
        (dated["noticeId"],),
    ).fetchone()
    days = workspace._days_between("2026-09-01", deadline) + 1  # the window reaches it
    won = workspace.new_pursuit(conn, "Won")
    workspace.set_outcome(conn, won.pursuit_id, "won", "x")  # post-award is not gated

    board = workspace.dashboard(conn, days=days, stall_days=14, horizon_days=60)

    assert [(w.due, w.kind, w.what, w.overdue) for w in board.work] == [
        ("2026-08-30", "task", "Late", True),
        ("2026-09-03", "task", "Call", False),
        (deadline, "response", f"response due: {dated['title']}", False),
    ]
    assert [(a.reason, a.pursuit_title) for a in board.attention] == [
        ("gate ready", "Ready"),
        ("hold due", "Held"),
        ("stalled", "Stale"),
    ]
    assert [(d.kind, d.pursuit_title) for d in board.dates] == [("response", "With a notice")]
    assert board.by_stage == (
        ("identify", 5), ("qualify", 0), ("capture", 0), ("proposal", 0), ("submitted", 0),
        ("post-award", 1),
    )  # fmt: skip
    assert noticed.stage == "identify" and stale.stage == "identify"


def test_assessments_are_stored_with_provenance_and_never_changed(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-07T20:00:00Z")
    p = workspace.new_pursuit(conn, "X")
    assert workspace.latest_assessment(conn, p.pursuit_id) is None
    assert workspace.pursuit(conn, p.pursuit_id).assessment is None
    first = workspace.save_assessment(
        conn, p.pursuit_id, slot="fast", provider="openai", model="qwen3:14b", prompt_version=1,
        profile_version=None, inputs_hash="abc", input_tokens=100, output_tokens=20,
        raw_response='{"fit": 40}', result={"fit": 40, "decision": "hold"},
    )  # fmt: skip
    second = workspace.save_assessment(
        conn, p.pursuit_id, slot="deep", provider="anthropic", model="claude-opus-5",
        prompt_version=1, profile_version=3, inputs_hash="def", input_tokens=None,
        output_tokens=None, raw_response="{}", result={"fit": 70, "decision": "go"},
    )  # fmt: skip
    assert first.assessment_id < second.assessment_id and first.created_at == "2026-09-07T20:00:00Z"
    assert workspace.latest_assessment(conn, p.pursuit_id) == second
    assert workspace.pursuit(conn, p.pursuit_id).assessment == second
    assert [a.result["fit"] for a in workspace.assessments(conn, p.pursuit_id)] == [70, 40]
    assert second.profile_version == 3 and second.result["decision"] == "go"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE assessments SET result = '{}'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM assessments")
    conn.execute("INSERT INTO users (user_id, name) VALUES (2, 'other')")
    assert workspace.latest_assessment(conn, p.pursuit_id, user_id=2) is None
    with pytest.raises(workspace.NotFound):
        workspace.assessments(conn, p.pursuit_id, user_id=2)
    with pytest.raises(workspace.NotFound):
        workspace.save_assessment(
            conn, 999, slot="fast", provider="openai", model="m", prompt_version=1,
            profile_version=None, inputs_hash="x", input_tokens=None, output_tokens=None,
            raw_response="", result={},
        )  # fmt: skip


def an_entity(conn: sqlite3.Connection) -> int:
    (entity_id,) = conn.execute(
        "INSERT INTO entities (kind, name, source_id, first_seen_at, last_seen_at)"
        " VALUES ('office', 'NETWORK CONTRACT OFFICE 16', 'sam_opportunities_api', ?, ?)"
        " RETURNING entity_id",
        (db.utcnow(), db.utcnow()),
    ).fetchone()
    return entity_id


def a_person(conn: sqlite3.Connection, entity_id: int) -> int:
    (person_id,) = conn.execute(
        "INSERT INTO people (name, role_title, entity_id, source_id, first_seen_at, last_seen_at)"
        " VALUES ('A Contracting Officer', 'CO', ?, 'sam_opportunities_api', ?, ?)"
        " RETURNING person_id",
        (entity_id, db.utcnow(), db.utcnow()),
    ).fetchone()
    return person_id


def test_a_task_can_be_about_an_entity_a_person_or_nothing(conn: sqlite3.Connection) -> None:
    entity_id = an_entity(conn)
    person_id = a_person(conn, entity_id)

    office = workspace.add_task(
        conn, "Call the CO before the industry day", subject_type="entity", subject_id=entity_id
    )
    contact = workspace.add_task(
        conn, "Ask which vehicle the follow-on uses", subject_type="person", subject_id=person_id
    )
    alone = workspace.add_task(conn, "Renew the SAM registration", due="2026-10-01")

    assert (office.subject_type, office.subject_id, office.pursuit_id, office.stage) == (
        "entity",
        str(entity_id),
        None,
        None,
    )
    assert (contact.subject_type, contact.subject_id) == ("person", str(person_id))
    assert (alone.subject_type, alone.subject_id, alone.origin) == (None, None, "user")
    assert [t.task_id for t in workspace.tasks(conn)] == [alone.task_id, office.task_id,
                                                          contact.task_id]  # fmt: skip
    assert [t.task_id for t in workspace.tasks(conn, subject_type="entity")] == [office.task_id]
    done = workspace.complete_task(conn, office.task_id)
    assert done.done_at is not None
    assert [t.task_id for t in workspace.tasks(conn)] == [alone.task_id, contact.task_id]


def test_a_task_off_a_pursuit_records_no_pursuit_history(conn: sqlite3.Connection) -> None:
    """pursuit_events is pursuit-scoped and append-only, so a task about an office writes
    nothing to it and does not touch any pursuit's updated_at."""
    p = workspace.new_pursuit(conn, "Help desk recompete")
    before = workspace.pursuit(conn, p.pursuit_id).pursuit.updated_at
    events_before = pursuit_events(conn, p.pursuit_id)

    task = workspace.add_task(conn, "Check the registration", subject_type="entity",
                              subject_id=an_entity(conn))  # fmt: skip
    workspace.complete_task(conn, task.task_id)

    after = workspace.pursuit(conn, p.pursuit_id)
    assert after.pursuit.updated_at == before
    assert pursuit_events(conn, p.pursuit_id) == events_before
    assert [t.task_id for t in after.tasks] != [task.task_id]


def test_a_subject_is_checked_before_a_task_is_written(conn: sqlite3.Connection) -> None:
    with pytest.raises(workspace.NotFound, match="no entity 404"):
        workspace.add_task(conn, "x", subject_type="entity", subject_id=404)
    with pytest.raises(workspace.NotFound, match="no person 404"):
        workspace.add_task(conn, "x", subject_type="person", subject_id=404)
    with pytest.raises(workspace.NotFound, match="no pursuit 404"):
        workspace.add_task(conn, "x", subject_type="pursuit", subject_id=404)
    with pytest.raises(ValueError, match="cannot be about"):
        workspace.add_task(conn, "x", subject_type="notice", subject_id=1)
    with pytest.raises(ValueError, match="both a kind and an id"):
        workspace.add_task(conn, "x", subject_type="entity")
    with pytest.raises(ValueError, match="both a kind and an id"):
        workspace.add_task(conn, "x", subject_id=1)
    with pytest.raises(ValueError, match="workflow stage"):
        workspace.add_task(conn, "x", subject_type="entity", subject_id=an_entity(conn),
                           stage="identify")  # fmt: skip
    assert workspace.tasks(conn) == ()


def test_another_users_pursuit_is_not_a_subject(conn: sqlite3.Connection) -> None:
    p = workspace.new_pursuit(conn, "Help desk recompete")
    with pytest.raises(workspace.NotFound):
        workspace.add_task(conn, "x", subject_type="pursuit", subject_id=p.pursuit_id, user_id=2)


def test_precedence_is_assigned_and_unset_is_not_routine(conn: sqlite3.Connection) -> None:
    entity_id = an_entity(conn)
    unset = workspace.add_task(conn, "whenever", subject_type="entity", subject_id=entity_id)
    flash = workspace.add_task(conn, "today", subject_type="entity", subject_id=entity_id,
                               precedence="flash")  # fmt: skip
    routine = workspace.add_task(conn, "sometime", subject_type="entity", subject_id=entity_id,
                                 precedence="routine")  # fmt: skip

    assert (unset.precedence, flash.precedence, routine.precedence) == (None, "flash", "routine")
    assert [t.task_id for t in workspace.tasks(conn)] == [
        flash.task_id, routine.task_id, unset.task_id
    ]  # fmt: skip
    assert workspace.precedence_rank(None) > workspace.precedence_rank("routine")

    with pytest.raises(ValueError, match="precedence must be"):
        workspace.add_task(conn, "x", precedence="urgent")
    with pytest.raises(ValueError, match="precedence must be"):
        workspace.set_task_precedence(conn, unset.task_id, "urgent")
    assert workspace.set_task_precedence(conn, unset.task_id, "priority").precedence == "priority"
    assert workspace.set_task_precedence(conn, unset.task_id, None).precedence is None
    with pytest.raises(workspace.NotFound):
        workspace.set_task_precedence(conn, 999, "flash")
