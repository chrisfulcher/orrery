"""Offices resolved against the SAM.gov Federal Hierarchy, one keyed request at a time.

An office reaches the store as a segment of an agency path: a code, and whatever name the
notice or the extract row that first created it happened to carry. The Federal Hierarchy API
knows the same offices under the same FPDS office code -- the AAC, the last segment of the
path -- and gives each a stable organization id, a canonical name, the names it used to go
by, and its place under a department. That is the identity the store has been missing.

The shape of the work is set by the quota, not by the hierarchy: there is no bulk download,
the filter takes one office code, and a personal key without a role gets ten requests a day.
So this is a per-office, budget-capped, resumable job and never a crawl. Each run takes the
offices nobody has resolved yet, spends at most ``budget`` requests on them, and leaves the
rest for tomorrow; an office the API does not know is marked so it is not asked about again
for a month, rather than consuming a request a day forever.

Two offices in the store can be the same real office: the bulk extract cannot reproduce a
deep Defense hierarchy, so it writes a three-segment path where the API writes a longer one.
They share an AAC and therefore resolve to one organization. The deeper row carries the
identity and the shallower one gets an ``fh.same_as`` fact pointing at it, which keeps the
unique index honest and makes the merge a sourced, reversible observation rather than a
deletion (§8, and the identity layer is append-only). Serves docs/DESIGN.md §5 source 5.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from orrery import db, quota, runs
from orrery.config import Settings
from orrery.ingest.notices import record_alias
from orrery.progress import Cancelled, Report, check, never, quiet
from orrery.sam.client import SamClient

SOURCE_ID = "sam_federal_hierarchy"
METHOD = "api"
RETRY_NOT_FOUND_DAYS = 30
"""How long a 'the hierarchy does not know this office' answer stands.

Long enough that a store full of offices the API has never heard of does not spend the whole
daily budget re-asking, short enough that an organization added later is found without a
maintenance chore. The marker is a fact, so what was asked and when is on the record.
"""

MIN_SEGMENTS = 3
"""Paths with fewer segments are a department or a sub-tier, not an office with an AAC."""


@dataclass(frozen=True)
class Office:
    """One office waiting to be resolved, and the code it will be looked up by."""

    entity_id: int
    name: str
    path_code: str

    @property
    def aac(self) -> str:
        return self.path_code.rsplit(".", 1)[1]


@dataclass(frozen=True)
class HierarchyResult:
    run_id: int
    offices_pending: int
    looked_up: int
    resolved: int
    twins_linked: int
    ambiguous: int
    not_found: int
    requests_spent: int
    budget_exhausted: bool


def pending_offices(conn: sqlite3.Connection, *, now: str | None = None) -> list[Office]:
    """Offices worth spending a request on, deepest path first.

    An office qualifies when its path can carry an AAC at all, nothing has resolved it, no
    twin of it has been linked to one that is, and the hierarchy has not said within the
    retry window that it does not know the code. Deepest first so that when twins are in the
    same run the row that can hold the identity is reached before the row that cannot.
    """
    cutoff = _days_ago(now or db.utcnow(), RETRY_NOT_FOUND_DAYS)
    rows = conn.execute(
        "SELECT entity_id, name, agency_path_code FROM entities AS e"
        " WHERE kind = 'office' AND agency_path_code IS NOT NULL"
        "   AND length(agency_path_code) - length(replace(agency_path_code, '.', '')) >= ?"
        "   AND fh_org_id IS NULL"
        "   AND NOT EXISTS (SELECT 1 FROM facts AS f"
        "        WHERE f.subject_type = 'entity' AND f.subject_id = CAST(e.entity_id AS TEXT)"
        "          AND (f.predicate = 'fh.same_as'"
        "               OR (f.predicate = 'fh.lookup' AND f.observed_at >= ?)))"
        " ORDER BY length(agency_path_code) DESC, entity_id",
        (MIN_SEGMENTS - 1, cutoff),
    ).fetchall()
    return [Office(*row) for row in rows]


def ingest_hierarchy(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    budget: int = 5,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> HierarchyResult:
    """Resolve up to ``budget`` offices against the Federal Hierarchy, one request each.

    Stopping is normal and is not a failure: the run ends ``succeeded`` whether it spent its
    whole budget, ran out of offices, or hit the daily quota mid-way, because every office it
    did resolve stands and the rest are simply still pending. Only an unexpected error fails
    the run.
    """
    pending = pending_offices(conn)
    run_id = runs.start(conn, SOURCE_ID)
    now = db.utcnow()
    looked_up = resolved = twins = ambiguous = not_found = 0
    budget_exhausted = False
    handled: set[int] = set()
    try:
        with SamClient(settings, conn, run_id) as sam:
            for office in pending:
                if looked_up >= budget:
                    break
                if office.entity_id in handled:
                    continue  # a twin already resolved it, and it cost one request
                check(cancelled)
                try:
                    orgs = sam.get_organizations(old_fpds_office_code=office.aac)
                except quota.BudgetExceeded as exc:
                    budget_exhausted = True
                    report(f"hierarchy: stopping, {exc}")
                    break
                looked_up += 1
                matches = _matching(orgs, office.aac)
                report(
                    f"hierarchy: {office.aac} answered with {len(orgs)} organization(s),"
                    f" {len(matches)} carrying this office code"
                )
                conn.execute("BEGIN")
                if len(matches) == 1:
                    holder, linked = _resolve(conn, office, matches[0], now)
                    handled |= linked | {holder}
                    resolved += 1
                    twins += len(linked)
                elif matches:
                    _record_candidates(conn, office, matches, now)
                    ambiguous += 1
                else:
                    _record_not_found(conn, office, now)
                    not_found += 1
                conn.execute("COMMIT")
                handled.add(office.entity_id)
    except Exception as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        runs.finish(conn, run_id, status="failed", records_returned=resolved, error=str(exc))
        raise
    runs.finish(conn, run_id, status="succeeded", records_returned=resolved)
    (spent,) = conn.execute(
        "SELECT requests_spent FROM ingestion_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    return HierarchyResult(
        run_id, len(pending), looked_up, resolved, twins, ambiguous, not_found,
        spent, budget_exhausted,
    )  # fmt: skip


def _matching(orgs: list[dict], aac: str) -> list[dict]:
    """The organizations that really carry this office code, active ones preferred.

    The filter is the server's, but the answer is checked rather than trusted: what comes
    back must say ``oldfpdsofficecode`` is the code we asked about, or it is not this office.
    An office that was reorganized can appear twice, once active and once not; the active one
    is the answer, and only a tie among equals is genuinely ambiguous.
    """
    matches = [org for org in orgs if _text(org.get("oldfpdsofficecode")) == aac]
    active = [org for org in matches if (_text(org.get("status")) or "").lower() == "active"]
    return active or matches


def _resolve(conn: sqlite3.Connection, office: Office, org: dict, now: str) -> tuple[int, set[int]]:
    """Write the identity onto the office (or its deepest twin) and link the rest to it.

    Returns (holder entity id, the twins linked to it). The holder is whichever row already
    carries this organization id, else the deepest path sharing the AAC: the deep row is the
    one the API itself would produce, so it is the one that keeps the key.
    """
    org_id = _text(org.get("fhorgid"))
    group = _twins(conn, office)
    holder = next((row for row in group if row[3] == org_id), None) or max(
        group, key=lambda row: (len(row[2]), row[0])
    )
    holder_id, holder_name = holder[0], holder[1]
    canonical = _text(org.get("fhorgname")) or holder_name
    conn.execute(
        "UPDATE entities SET fh_org_id = ?, old_fpds_office_code = ?, name = ?,"
        " last_seen_at = ? WHERE entity_id = ?",
        (org_id, office.aac, canonical, now, holder_id),
    )
    # The name the office arrived with is not wrong, it is what a source called it, so it
    # stays reachable as an alias rather than being overwritten out of existence.
    for alias in [holder_name, *_name_history(org)]:
        if alias and alias != canonical:
            record_alias(conn, holder_id, alias, SOURCE_ID, now)
    _write_facts(conn, holder_id, _facts(org, org_id), org_id, now)
    linked = set()
    for entity_id, _, _, _ in group:
        if entity_id == holder_id:
            continue
        conn.execute(
            "UPDATE entities SET old_fpds_office_code = ? WHERE entity_id = ?",
            (office.aac, entity_id),
        )
        _write_facts(conn, entity_id, [("fh.same_as", str(holder_id), "ref")], org_id, now)
        linked.add(entity_id)
    return holder_id, linked


def _twins(conn: sqlite3.Connection, office: Office) -> list[tuple[int, str, str, str | None]]:
    """Every office entity whose path ends in this AAC, the office itself included."""
    return conn.execute(
        "SELECT entity_id, name, agency_path_code, fh_org_id FROM entities"
        " WHERE kind = 'office' AND substr(agency_path_code, -length(?) - 1) = '.' || ?",
        (office.aac, office.aac),
    ).fetchall()


def _record_candidates(
    conn: sqlite3.Connection, office: Office, matches: list[dict], now: str
) -> None:
    """Several organizations claim this office code, so the office stays unresolved.

    Guessing here would write a wrong identity that every later source inherits. The
    candidates are recorded instead, each under its own organization id, so the choice can be
    made by a person or by a later rule with the evidence in front of it.
    """
    for org in matches:
        org_id = _text(org.get("fhorgid"))
        _write_facts(conn, office.entity_id, [("fh.candidate", _json(org), "text")], org_id, now)


def _record_not_found(conn: sqlite3.Connection, office: Office, now: str) -> None:
    """The hierarchy knows no organization under this code. A fact, so the next run can see
    that the question was asked and does not spend a request asking it again."""
    _write_facts(conn, office.entity_id, [("fh.lookup", "not found", "text")], None, now)


def _facts(org: dict, org_id: str | None) -> list[tuple[str, str, str]]:
    """(predicate, value, value_type) for what the hierarchy says about this organization.

    The whole record is kept alongside the typed pieces, as every adapter here keeps its raw
    row: the field list is documented but this release has not seen a live response, so a
    field read wrongly is recoverable from what was stored rather than from a second request.
    """
    single = (
        ("fh.org_id", org_id, "text"),
        ("fh.org_name", _text(org.get("fhorgname")), "text"),
        ("fh.org_type", _text(org.get("fhorgtype")), "text"),
        ("fh.agency_code", _text(org.get("agencycode")), "text"),
        ("fh.status", _text(org.get("status")), "text"),
    )
    facts = [(p, v, t) for p, v, t in single if v]
    history = org.get("fhorgparenthistory")
    if history:
        facts.append(("fh.parent_history", _json(history), "text"))
    facts.append(("fh.record", _json(org), "text"))
    return facts


def _write_facts(
    conn: sqlite3.Connection,
    entity_id: int,
    facts: list[tuple[str, str, str]],
    source_ref: str | None,
    now: str,
) -> None:
    conn.executemany(
        "INSERT INTO facts (subject_type, subject_id, predicate, value_type, value, source_id,"
        " source_ref, observed_at, confidence, extraction_method)"
        f" VALUES ('entity', ?, ?, ?, ?, '{SOURCE_ID}', ?, ?, 1.0, '{METHOD}')",
        [
            (str(entity_id), predicate, value_type, value, source_ref, now)
            for predicate, value, value_type in facts
        ],
    )


def _name_history(org: dict) -> list[str]:
    """Every name this organization has gone by, as aliases the store can be searched under.

    Entries are documented as objects; a bare string is accepted too, because the envelope
    around these fields has not been seen live and a name is worth keeping either way.
    """
    names = []
    for entry in org.get("fhorganamehistory") or []:
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            name = _text(entry.get("fhorgname")) or ""
        else:
            continue
        if name and name not in names:
            names.append(name)
    return names


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _days_ago(now: str, days: int) -> str:
    return (datetime.strptime(now, db.TIMESTAMP_FORMAT) - timedelta(days=days)).strftime(
        db.TIMESTAMP_FORMAT
    )
