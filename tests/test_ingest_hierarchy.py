"""Offices resolved against the Federal Hierarchy.

Every organization here is invented. The envelope the fixture is built from is not verified
against the live API (see the note inside `fixtures/sam_fh_orgs_v1.json`), so these tests
pin the adapter's behaviour given a shape, not the shape itself.
"""

import json
import re
import sqlite3
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from orrery import db, query
from orrery.config import Settings
from orrery.ingest.hierarchy import ingest_hierarchy, pending_offices

FIXTURE = json.loads((Path(__file__).with_name("fixtures") / "sam_fh_orgs_v1.json").read_text())
TEMPLATE = FIXTURE["orglist"][0]


def orgs_url(aac: str) -> re.Pattern[str]:
    return re.compile(rf".*/prod/federalorganizations/v1/orgs\?.*oldfpdsofficecode={aac}.*")


def org(
    *,
    aac: str = "EX0001",
    org_id: str = "100000001",
    name: str = "EXAMPLE CONTRACTING OFFICE 01",
    status: str = "Active",
) -> dict:
    return {
        **TEMPLATE,
        "oldfpdsofficecode": aac,
        "fhorgid": org_id,
        "fhorgname": name,
        "status": status,
    }


def envelope(*organizations: dict) -> dict:
    return {"totalrecords": len(organizations), "orglist": list(organizations)}


def make_office(conn: sqlite3.Connection, path_code: str, name: str) -> int:
    """An office as the notice and bulk adapters leave it: a path code and a source's name."""
    (entity_id,) = conn.execute(
        "INSERT INTO entities (kind, name, agency_path_code, source_id, first_seen_at,"
        " last_seen_at) VALUES ('office', ?, ?, 'sam_opportunities_api', ?, ?)"
        " RETURNING entity_id",
        (name, path_code, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ).fetchone()
    return entity_id


def freeze(monkeypatch: pytest.MonkeyPatch, stamp: str) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: stamp)


def facts_of(conn: sqlite3.Connection, entity_id: int) -> dict[str, str]:
    return dict(
        conn.execute(
            "SELECT predicate, value FROM facts WHERE subject_type = 'entity' AND subject_id = ?",
            (str(entity_id),),
        ).fetchall()
    )


def test_an_office_takes_the_hierarchys_name_and_keys(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    office = make_office(conn, "097.97DH.EX0001", "OFFICE AS THE NOTICE NAMED IT")
    httpx_mock.add_response(url=orgs_url("EX0001"), json=envelope(org()))

    result = ingest_hierarchy(conn, settings, budget=5)

    assert (result.offices_pending, result.looked_up, result.resolved) == (1, 1, 1)
    assert (result.twins_linked, result.ambiguous, result.not_found) == (0, 0, 0)
    assert result.requests_spent == 1 and result.budget_exhausted is False
    assert conn.execute(
        "SELECT name, fh_org_id, old_fpds_office_code FROM entities WHERE entity_id = ?",
        (office,),
    ).fetchone() == ("EXAMPLE CONTRACTING OFFICE 01", "100000001", "EX0001")
    # The name the office arrived with is not wrong, only less canonical, so it stays
    # findable; so does every name the organization has gone by.
    aliases = conn.execute(
        "SELECT alias, method, confidence FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
        (office,),
    ).fetchall()
    assert aliases == [
        ("EXAMPLE CONTRACTING OFFICE (FORMER)", "exact_key", 1.0),
        ("OFFICE AS THE NOTICE NAMED IT", "exact_key", 1.0),
    ]
    facts = facts_of(conn, office)
    assert facts["fh.org_id"] == "100000001"
    assert facts["fh.org_name"] == "EXAMPLE CONTRACTING OFFICE 01"
    assert facts["fh.org_type"] == "Office"
    assert facts["fh.agency_code"] == "9999"
    assert facts["fh.status"] == "Active"
    assert json.loads(facts["fh.parent_history"])[0]["fhorgname"] == "EXAMPLE DEPARTMENT"
    # The whole record is kept: the field list is documented but unverified, so a field read
    # wrongly has to be recoverable without spending the day's quota again.
    assert json.loads(facts["fh.record"])["fhdeptindagencyorgid"] == "100000000"
    assert conn.execute(
        "SELECT DISTINCT source_id, source_ref, extraction_method, confidence FROM facts"
        " WHERE subject_id = ?",
        (str(office),),
    ).fetchall() == [("sam_federal_hierarchy", "100000001", "api", 1.0)]
    [endpoint] = [row[0] for row in conn.execute("SELECT endpoint FROM api_requests")]
    assert "oldfpdsofficecode=EX0001" in endpoint and "api_key" not in endpoint


def test_a_resolved_office_is_never_asked_about_again(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    make_office(conn, "097.97DH.EX0001", "OFFICE AS THE NOTICE NAMED IT")
    httpx_mock.add_response(url=orgs_url("EX0001"), json=envelope(org()))
    ingest_hierarchy(conn, settings, budget=5)

    again = ingest_hierarchy(conn, settings, budget=5)

    assert (again.offices_pending, again.looked_up, again.requests_spent) == (0, 0, 0)
    assert len(httpx_mock.get_requests()) == 1


def test_a_department_or_sub_tier_is_never_looked_up(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    """Only the last segment of a three-segment path is an AAC; there is nothing to ask
    about a department, and asking would spend a tenth of the day's requests to find out."""
    conn.execute(
        "INSERT INTO entities (kind, name, agency_path_code, source_id, first_seen_at,"
        " last_seen_at) VALUES ('agency', 'A DEPARTMENT', '097', 'sam_opportunities_api',"
        " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    make_office(conn, "097.97DH", "A SUB-TIER")

    result = ingest_hierarchy(conn, settings, budget=5)

    assert (result.offices_pending, result.looked_up) == (0, 0)
    assert httpx_mock.get_requests() == []


def test_an_office_the_hierarchy_does_not_know_is_not_asked_about_daily(
    httpx_mock: HTTPXMock,
    conn: sqlite3.Connection,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    office = make_office(conn, "097.97DH.EX0404", "UNKNOWN OFFICE")
    httpx_mock.add_response(url=orgs_url("EX0404"), json={"totalrecords": 0})
    freeze(monkeypatch, "2026-09-14T00:00:00Z")

    first = ingest_hierarchy(conn, settings, budget=5)

    assert (first.resolved, first.not_found) == (0, 1)
    assert facts_of(conn, office) == {"fh.lookup": "not found"}

    freeze(monkeypatch, "2026-10-13T00:00:00Z")  # 29 days on: the answer still stands
    assert ingest_hierarchy(conn, settings, budget=5).looked_up == 0
    assert len(httpx_mock.get_requests()) == 1

    freeze(monkeypatch, "2026-10-15T00:00:00Z")  # 31 days on: worth one more request
    httpx_mock.add_response(url=orgs_url("EX0404"), json=envelope(org(aac="EX0404")))
    third = ingest_hierarchy(conn, settings, budget=5)
    assert (third.looked_up, third.resolved) == (1, 1)


def test_an_organization_that_does_not_carry_the_code_is_not_this_office(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    """The filter is the server's; the answer is checked. An envelope full of neighbours is
    the same outcome as an empty one, and neither writes an identity."""
    office = make_office(conn, "097.97DH.EX0001", "OFFICE AS THE NOTICE NAMED IT")
    httpx_mock.add_response(url=orgs_url("EX0001"), json=envelope(org(aac="EX9999")))

    result = ingest_hierarchy(conn, settings, budget=5)

    assert (result.resolved, result.not_found) == (0, 1)
    assert conn.execute(
        "SELECT name, fh_org_id FROM entities WHERE entity_id = ?", (office,)
    ).fetchone() == ("OFFICE AS THE NOTICE NAMED IT", None)


def test_twins_cost_one_request_and_the_shallow_one_points_at_the_deep_one(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    deep = make_office(conn, "097.97DH.0097.EX0001", "DEEP TWIN")
    shallow = make_office(conn, "097.9700.EX0001", "SHALLOW TWIN")
    httpx_mock.add_response(url=orgs_url("EX0001"), json=envelope(org()))

    result = ingest_hierarchy(conn, settings, budget=5)

    assert (result.offices_pending, result.looked_up) == (2, 1)
    assert (result.resolved, result.twins_linked) == (1, 1)
    # The unique index is the reason only one row may hold the id; the fact is the reason
    # the other row is not simply deleted.
    assert conn.execute(
        "SELECT fh_org_id, old_fpds_office_code FROM entities WHERE entity_id = ?", (deep,)
    ).fetchone() == ("100000001", "EX0001")
    assert conn.execute(
        "SELECT fh_org_id, old_fpds_office_code FROM entities WHERE entity_id = ?", (shallow,)
    ).fetchone() == (None, "EX0001")
    assert conn.execute(
        "SELECT value, value_type, source_ref FROM facts WHERE subject_id = ?"
        " AND predicate = 'fh.same_as'",
        (str(shallow),),
    ).fetchone() == (str(deep), "ref", "100000001")
    # Their paths diverge above the sub-tier, so the old heuristic gave up here.
    resolved = query.office_for_code(conn, "EX0001")
    assert resolved is not None and resolved.entity_id == deep
    assert ingest_hierarchy(conn, settings, budget=5).looked_up == 0
    assert len(httpx_mock.get_requests()) == 1


def test_the_active_organization_wins_over_a_retired_one(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    office = make_office(conn, "097.97DH.EX0003", "REORGANIZED OFFICE")
    httpx_mock.add_response(
        url=orgs_url("EX0003"),
        json=envelope(
            org(aac="EX0003", org_id="300000001", name="RETIRED NAME", status="Inactive"),
            org(aac="EX0003", org_id="300000002", name="CURRENT NAME", status="Active"),
        ),
    )

    result = ingest_hierarchy(conn, settings, budget=5)

    assert (result.resolved, result.ambiguous) == (1, 0)
    assert conn.execute(
        "SELECT fh_org_id, name FROM entities WHERE entity_id = ?", (office,)
    ).fetchone() == ("300000002", "CURRENT NAME")


def test_two_live_organizations_claiming_one_code_resolve_nothing(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    """Guessing would write an identity every later source inherits, so the candidates are
    recorded and the office stays unresolved for a person to settle."""
    office = make_office(conn, "097.97DH.EX0002", "AMBIGUOUS OFFICE")
    httpx_mock.add_response(
        url=orgs_url("EX0002"),
        json=envelope(
            org(aac="EX0002", org_id="200000001", name="FIRST CLAIMANT"),
            org(aac="EX0002", org_id="200000002", name="SECOND CLAIMANT"),
        ),
    )

    result = ingest_hierarchy(conn, settings, budget=5)

    assert (result.resolved, result.ambiguous, result.not_found) == (0, 1, 0)
    assert conn.execute(
        "SELECT fh_org_id, name FROM entities WHERE entity_id = ?", (office,)
    ).fetchone() == (None, "AMBIGUOUS OFFICE")
    candidates = conn.execute(
        "SELECT source_ref, value FROM facts WHERE subject_id = ? AND predicate = 'fh.candidate'"
        " ORDER BY source_ref",
        (str(office),),
    ).fetchall()
    assert [ref for ref, _ in candidates] == ["200000001", "200000002"]
    assert json.loads(candidates[0][1])["fhorgname"] == "FIRST CLAIMANT"


def test_the_run_stops_at_its_own_budget_and_leaves_the_rest_pending(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    make_office(conn, "097.97DH.EX0001", "FIRST OFFICE")
    make_office(conn, "097.97DH.EX0002", "SECOND OFFICE")
    httpx_mock.add_response(url=orgs_url("EX0001"), json=envelope(org()))

    result = ingest_hierarchy(conn, settings, budget=1)

    assert (result.offices_pending, result.looked_up, result.resolved) == (2, 1, 1)
    # Not budget_exhausted: the run stopped at the cap it was given, not at SAM.gov's, and
    # saying otherwise would tell the user the day's requests are gone when they are not.
    assert result.budget_exhausted is False
    assert len(httpx_mock.get_requests()) == 1
    assert [office.aac for office in pending_offices(conn)] == ["EX0002"]


def test_the_daily_quota_stops_the_run_without_failing_it(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    """A run that meets the quota mid-way has still done real work: the offices it resolved
    stand, the rest are simply still pending, and tomorrow's run continues."""
    make_office(conn, "097.97DH.EX0001", "FIRST OFFICE")
    make_office(conn, "097.97DH.EX0002", "SECOND OFFICE")
    httpx_mock.add_response(url=orgs_url("EX0001"), json=envelope(org()))
    one = settings.model_copy(update={"sam_daily_budget": 1})

    result = ingest_hierarchy(conn, one, budget=5)

    assert (result.looked_up, result.resolved, result.budget_exhausted) == (1, 1, True)
    assert conn.execute(
        "SELECT status FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
    ).fetchone() == ("succeeded",)
    assert len(httpx_mock.get_requests()) == 1
    assert len(pending_offices(conn)) == 1


def test_a_spent_quota_resolves_nothing_and_still_succeeds(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    make_office(conn, "097.97DH.EX0001", "FIRST OFFICE")
    none_left = settings.model_copy(update={"sam_daily_budget": 0})

    result = ingest_hierarchy(conn, none_left, budget=5)

    assert (result.looked_up, result.resolved, result.budget_exhausted) == (0, 0, True)
    assert httpx_mock.get_requests() == []
    assert conn.execute(
        "SELECT status FROM ingestion_runs WHERE run_id = ?", (result.run_id,)
    ).fetchone() == ("succeeded",)
    assert len(pending_offices(conn)) == 1


def test_the_key_goes_to_the_api_host_and_nowhere_else(
    httpx_mock: HTTPXMock, conn: sqlite3.Connection, settings: Settings
) -> None:
    make_office(conn, "097.97DH.EX0001", "FIRST OFFICE")
    httpx_mock.add_response(url=orgs_url("EX0001"), json=envelope(org()))

    ingest_hierarchy(conn, settings, budget=1)

    keyed = [sent for sent in httpx_mock.get_requests() if "api_key" in sent.url.params]
    assert {sent.url.host for sent in keyed} == {"api.sam.gov"}
    assert all(
        "api_key" not in endpoint
        for (endpoint,) in conn.execute("SELECT endpoint FROM api_requests")
    )
