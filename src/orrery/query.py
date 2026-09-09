"""The one query module every front end reads through (CLAUDE.md Interfaces; DESIGN.md §4).

Search unions hits from the notice text index and the attachment text index, keeps the
best-ranked hit per notice, and labels it with its source. bm25 scores from two tables are
not one scale; treating them as comparable is a ranking heuristic, not a measurement.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from orrery import db, naics


class InvalidQuery(ValueError):
    """The text is not valid FTS5 syntax even after quoting each token."""


@dataclass(frozen=True)
class SearchHit:
    notice_id: str
    title: str
    agency: str | None
    response_deadline: str | None
    posted_at: str | None
    source: str
    """'notice' for the notice's own text, otherwise the attachment filename."""
    snippet: str
    rank: float
    """bm25 for keyword hits, cosine distance for semantic hits; lower is better in both."""
    page: int | None = None
    """1-based page of an attachment hit; None for notice text and for keyword hits."""
    set_aside_code: str | None = None
    summary: str | None = None
    """The latest stored summary of the notice (``orrery summarize``), when one exists."""
    work_type: str | None = None
    stated_set_aside: str | None = None
    """The set-aside the notice text states, from the summary; fills an empty code."""


@dataclass(frozen=True)
class Filters:
    """Structured predicates over notices; None means any. Saved searches store one of these."""

    naics: tuple[str, ...] | None = None
    set_asides: tuple[str, ...] | None = None
    agency_prefixes: tuple[str, ...] | None = None
    """``full_parent_path_code`` prefixes, matched on dot boundaries."""
    deadline_within_days: int | None = None
    active_only: bool = False

    def params(self) -> dict[str, object]:
        return {
            "naics": json.dumps(self.naics) if self.naics else None,
            "set_asides": json.dumps(self.set_asides) if self.set_asides else None,
            "agency_path_prefixes": (
                json.dumps(self.agency_prefixes) if self.agency_prefixes else None
            ),
            "deadline_within_days": self.deadline_within_days,
            "active_only": int(self.active_only),
            "now": db.utcnow(),
        }


NO_FILTERS = Filters()


def notice_filter_sql(src: str) -> str:
    """Predicates on ``notices AS n`` reading filter values from ``src``: ``:`` for bound
    parameters, or a table alias such as ``s.`` whose columns carry the same names."""
    naics_match = naics.match_sql("n.naics_code", f"{src}naics")
    return f"""
    AND ({src}naics IS NULL OR {naics_match})
    AND ({src}set_asides IS NULL
         OR n.set_aside_code IN (SELECT value FROM json_each({src}set_asides)))
    AND ({src}agency_path_prefixes IS NULL OR EXISTS (
         SELECT 1 FROM json_each({src}agency_path_prefixes)
         WHERE n.full_parent_path_code = value OR n.full_parent_path_code LIKE value || '.%'))
    AND ({src}deadline_within_days IS NULL OR n.response_deadline BETWEEN :now AND
         strftime('%Y-%m-%dT%H:%M:%SZ', :now, '+' || {src}deadline_within_days || ' days'))
    """


FILTERS = notice_filter_sql(":") + " AND (:active_only = 0 OR n.active = 1)"

CONTRACT_NAICS_MATCH = naics.match_sql("naics_code", ":naics")
"""The slice predicate over ``v_contracts``, hoisted so that callers whose own parameter is
named ``naics`` can still reach it."""

SEARCH = f"""
WITH hits AS (
    SELECT n.id AS nid, 'notice' AS source, bm25(notices_fts) AS rank,
           snippet(notices_fts, -1, '[', ']', '...', 12) AS snippet
    FROM notices_fts JOIN notices AS n ON n.id = notices_fts.rowid
    WHERE notices_fts MATCH :q
    UNION ALL
    SELECT n.id, a.filename, bm25(attachments_fts),
           snippet(attachments_fts, 0, '[', ']', '...', 12)
    FROM attachments_fts
    JOIN attachments AS a ON a.attachment_id = attachments_fts.rowid
    JOIN notices AS n ON n.notice_id = a.notice_id
    WHERE attachments_fts MATCH :q
)
SELECT n.notice_id, n.title, e.name, n.response_deadline, n.posted_at,
       h.source, h.snippet, min(h.rank) AS rank,
       n.set_aside_code, s.summary, s.work_type, s.stated_set_aside
FROM hits AS h JOIN notices AS n ON n.id = h.nid
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
LEFT JOIN v_notice_summaries AS s ON s.notice_id = n.notice_id
WHERE 1 = 1 {FILTERS}
GROUP BY h.nid
ORDER BY rank, n.posted_at DESC
LIMIT :limit
"""

LIST_NOTICES = f"""
SELECT n.notice_id, n.title, e.name, n.response_deadline, n.posted_at,
       'notice' AS source, coalesce(n.description, '') AS snippet, 0.0 AS rank,
       n.set_aside_code, s.summary, s.work_type, s.stated_set_aside
FROM notices AS n LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
LEFT JOIN v_notice_summaries AS s ON s.notice_id = n.notice_id
WHERE 1 = 1 {FILTERS}
ORDER BY n.response_deadline IS NULL, n.response_deadline, n.posted_at DESC, n.id
LIMIT :limit
"""


SEMANTIC_SEARCH = """
WITH hits AS (
    SELECT notice_id, attachment_id, page, text, vec_distance_cosine(vector, :q) AS distance
    FROM embeddings WHERE model = :model
)
SELECT n.notice_id, n.title, e.name, n.response_deadline, n.posted_at,
       coalesce(a.filename, 'notice') AS source, h.text, min(h.distance) AS rank, h.page,
       n.set_aside_code, s.summary, s.work_type, s.stated_set_aside
FROM hits AS h
JOIN notices AS n ON n.notice_id = h.notice_id
LEFT JOIN attachments AS a ON a.attachment_id = h.attachment_id
LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id
LEFT JOIN v_notice_summaries AS s ON s.notice_id = n.notice_id
GROUP BY h.notice_id
ORDER BY rank, n.posted_at DESC
LIMIT :limit
"""


def semantic_search(
    conn: sqlite3.Connection, vector: bytes, *, model: str, limit: int = 20
) -> list[SearchHit]:
    """Nearest chunk per notice by cosine distance over rows embedded with ``model``.

    ``vector`` is the query embedding as a float32 blob (``orrery.embed.client.pack``); the
    caller embeds the query, so this module never touches the network. The connection must
    have ``db.load_vec`` applied.
    """
    rows = conn.execute(SEMANTIC_SEARCH, {"q": vector, "model": model, "limit": limit}).fetchall()
    return [_hit(row[:8] + row[9:], _snippet(row[6]), page=row[8]) for row in rows]


def _snippet(text: str, width: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width].rsplit(" ", 1)[0] + "..."


def _hit(row: tuple, snippet: str, *, page: int | None = None) -> SearchHit:
    """A hit from the eight standard columns followed by the notice's set-aside code and its
    latest summary's text, work type, and stated set-aside."""
    return SearchHit(
        *row[:6],
        snippet,
        row[7],
        page,
        set_aside_code=row[8],
        summary=row[9],
        work_type=row[10],
        stated_set_aside=row[11],
    )


def search(
    conn: sqlite3.Connection, text: str, *, limit: int = 20, filters: Filters = NO_FILTERS
) -> list[SearchHit]:
    """Full-text search across notice text and attachment text, best hit per notice.

    The text is passed to FTS5 as written first, so operators, prefixes, and phrases work;
    if FTS5 rejects it, every whitespace-separated token is retried as its own phrase, so
    plain queries such as ``wi-fi`` or ``section l/m`` work too. ``filters`` narrows the
    notices considered.
    """
    try:
        rows = _match(conn, text, limit, filters)
    except sqlite3.OperationalError:
        try:
            rows = _match(conn, _quoted(text), limit, filters)
        except sqlite3.OperationalError as exc:
            raise InvalidQuery(str(exc)) from exc
    return [_hit(row, " ".join(row[6].split())) for row in rows]


def list_notices(conn: sqlite3.Connection, filters: Filters, *, limit: int = 50) -> list[SearchHit]:
    """Notices passing the filters, soonest deadline first; no text ranking (rank is 0)."""
    rows = conn.execute(LIST_NOTICES, {"limit": limit, **filters.params()}).fetchall()
    return [_hit(row, _snippet(row[6])) for row in rows]


def rebuild_search(conn: sqlite3.Connection) -> None:
    """Rebuild both FTS indexes from their content tables."""
    conn.execute("INSERT INTO notices_fts(notices_fts) VALUES ('rebuild')")
    conn.execute("INSERT INTO attachments_fts(attachments_fts) VALUES ('rebuild')")


def _match(conn: sqlite3.Connection, query: str, limit: int, filters: Filters) -> list[tuple]:
    return conn.execute(SEARCH, {"q": query, "limit": limit, **filters.params()}).fetchall()


def _quoted(text: str) -> str:
    return " ".join('"' + token.replace('"', '""') + '"' for token in text.split())


# Detail reads for the context view, the entity view, the dashboard, and the MCP server.
# Public data only; tracking lives in orrery.workspace.


@dataclass(frozen=True)
class EntityRef:
    entity_id: int
    name: str
    path_code: str | None


@dataclass(frozen=True)
class AttachmentInfo:
    attachment_id: int
    filename: str | None
    url: str
    fetch_status: str
    extract_status: str
    path: str | None
    text_chars: int


@dataclass(frozen=True)
class ContractRef:
    """One award as ``v_contracts`` presents it: resolved names where the graph has them,
    the source's strings otherwise."""

    contract_id: int
    award_key: str
    piid: str
    parent_piid: str | None
    vendor: str | None
    vendor_entity_id: int | None
    vendor_uei: str | None
    awarding_office: str | None
    awarding_office_code: str | None
    awarding_entity_id: int | None
    value_usd: float | None
    potential_value_usd: float | None
    award_date: str | None
    last_action_date: str | None
    pop_end: str | None
    naics_code: str | None
    psc_code: str | None
    award_type_code: str | None
    set_aside_code: str | None
    extent_competed_code: str | None
    solicitation_identifier: str | None
    url: str | None
    pop_potential_end: str | None = None
    """The end of the period of performance with every option exercised."""


CONTRACT_COLUMNS = (
    "contract_id, award_key, piid, parent_piid, vendor, vendor_entity_id, vendor_uei,"
    " awarding_office, awarding_office_code, awarding_entity_id, value_usd, potential_value_usd,"
    " award_date, last_action_date, pop_end, naics_code, psc_code, award_type_code,"
    " set_aside_code, extent_competed_code, solicitation_identifier, url, pop_potential_end"
)


@dataclass(frozen=True)
class Official:
    """A government point of contact as the notice names them (official capacity only)."""

    name: str
    kind: str
    """'primary' or 'secondary'."""
    title: str | None
    email: str | None
    phone: str | None
    fax: str | None
    office_address: str | None
    other_notices: int
    """Other notices from the same office naming this contact."""


@dataclass(frozen=True)
class Fact:
    predicate: str
    value: str
    value_type: str
    observed_at: str
    source_id: str
    source_ref: str | None


@dataclass(frozen=True)
class NoticeDetail:
    notice_id: str
    solicitation_number: str | None
    title: str
    notice_type: str | None
    naics_code: str | None
    psc_code: str | None
    set_aside_code: str | None
    posted_at: str | None
    response_deadline: str | None
    active: bool
    first_seen_at: str
    last_seen_at: str
    source_id: str
    description_status: str
    description: str | None
    url: str | None
    agency: str | None
    agency_chain: tuple[EntityRef, ...]
    """Root first, leaf last."""
    attachments: tuple[AttachmentInfo, ...]
    versions: int
    award_number: str | None = None
    award_date: str | None = None
    award_amount: str | None = None
    awardee: str | None = None
    incumbent: ContractRef | None = None
    """The award this notice continues: same solicitation identifier, else same award number."""
    award_history: tuple[ContractRef, ...] = ()
    """Recent awards from the same office in the notice's NAICS, newest first."""
    officials: tuple[Official, ...] = ()
    summary: str | None = None
    """The latest stored summary (``orrery summarize``): two sentences, or None."""
    work_type: str | None = None
    keywords: tuple[str, ...] = ()
    stated_set_aside: str | None = None
    """The set-aside the notice text states, as a SAM.gov code; fills an empty code."""
    summary_model: str | None = None


@dataclass(frozen=True)
class EntityDetail:
    entity_id: int
    kind: str
    name: str
    path_code: str | None
    parent: EntityRef | None
    chain: tuple[EntityRef, ...]
    """Root first, this entity last."""
    children: tuple[EntityRef, ...]
    aliases: tuple[str, ...]
    notices: int
    """Notices under this entity or any office below it."""
    recent: tuple[SearchHit, ...]
    uei: str | None = None
    cage: str | None = None
    awards: tuple[ContractRef, ...] = ()
    """Won, for a contractor; made, for an office. Newest first."""
    awards_count: int = 0
    awards_value_usd: float = 0.0
    facts: tuple[Fact, ...] = ()
    """Every sourced fact about the entity, newest first."""


@dataclass(frozen=True)
class StoreCounts:
    notices: int
    active: int
    entities: int


CHAIN = """
WITH RECURSIVE chain(entity_id, name, agency_path_code, parent_entity_id, depth) AS (
    SELECT entity_id, name, agency_path_code, parent_entity_id, 0
    FROM entities WHERE entity_id = :id
    UNION ALL
    SELECT e.entity_id, e.name, e.agency_path_code, e.parent_entity_id, c.depth + 1
    FROM entities AS e JOIN chain AS c ON e.entity_id = c.parent_entity_id
)
SELECT entity_id, name, agency_path_code FROM chain ORDER BY depth DESC
"""


def notice(conn: sqlite3.Connection, notice_id: str) -> NoticeDetail | None:
    """Everything public about one notice, for the context view."""
    row = conn.execute(
        "SELECT notice_id, solicitation_number, title, notice_type, naics_code, psc_code,"
        " set_aside_code, posted_at, response_deadline, active, first_seen_at, last_seen_at,"
        " source_id, description_status, description, url, agency, agency_entity_id, versions,"
        " award_number, award_date, award_amount, awardee, agency_path_code,"
        " summary, work_type, keywords, stated_set_aside, summary_model"
        " FROM v_notices WHERE notice_id = ?",
        (notice_id,),
    ).fetchone()
    if row is None:
        return None
    office_code = row[23].rsplit(".", 1)[-1] if row[23] else None
    history = awards(conn, office_code=office_code, naics=row[4], limit=10) if office_code else []
    attachments = tuple(
        AttachmentInfo(*item)
        for item in conn.execute(
            "SELECT attachment_id, filename, url, fetch_status, extract_status, path,"
            " length(coalesce(extracted_text, '')) FROM attachments"
            " WHERE notice_id = ? ORDER BY attachment_id",
            (notice_id,),
        ).fetchall()
    )
    return NoticeDetail(
        *row[:9],
        bool(row[9]),
        *row[10:17],
        _chain(conn, row[17]),
        attachments,
        row[18],
        *row[19:23],
        _incumbent(conn, row[1], row[19], office_code),
        tuple(history),
        _officials(conn, notice_id, row[17]),
        summary=row[24],
        work_type=row[25],
        keywords=tuple(json.loads(row[26])) if row[26] else (),
        stated_set_aside=row[27],
        summary_model=row[28],
    )


def _incumbent(
    conn: sqlite3.Connection, solicitation: str | None, award_number: str | None, office: str | None
) -> ContractRef | None:
    """The award with this solicitation identifier, else with this PIID; the same office and the
    latest action break ties."""
    for column, value in (("solicitation_identifier", solicitation), ("piid", award_number)):
        if not value:
            continue
        row = conn.execute(
            f"SELECT {CONTRACT_COLUMNS} FROM v_contracts WHERE {column} = :value"
            " ORDER BY awarding_office_code = :office DESC, last_action_date DESC, contract_id DESC"
            " LIMIT 1",
            {"value": value, "office": office},
        ).fetchone()
        if row is not None:
            return ContractRef(*row)
    return None


def _officials(
    conn: sqlite3.Connection, notice_id: str, agency_entity_id: int | None
) -> tuple[Official, ...]:
    """The notice's points of contact, from the API's list or the extract's columns."""
    (raw,) = conn.execute(
        "SELECT raw_json FROM notices WHERE notice_id = ?", (notice_id,)
    ).fetchone()
    record = json.loads(raw)
    contacts: list[tuple[str, str | None, str | None, str | None, str | None, str | None]] = []
    if "pointOfContact" in record:
        address = record.get("officeAddress") or {}
        office = _address(address.get("city"), address.get("state"), address.get("zipcode"))
        for poc in record.get("pointOfContact") or []:
            if poc.get("fullName"):
                contacts.append(
                    (poc["fullName"], poc.get("type") or "primary", poc.get("title"),
                     poc.get("email"), poc.get("phone"), poc.get("fax"))
                )  # fmt: skip
    else:
        office = _address(record.get("City"), record.get("State"), record.get("ZipCode"))
        for prefix, kind in (("PrimaryContact", "primary"), ("SecondaryContact", "secondary")):
            name = (record.get(f"{prefix}Fullname") or "").strip()
            if name:
                contacts.append(
                    (name, kind, *(record.get(f"{prefix}{f}") or None
                                   for f in ("Title", "Email", "Phone", "Fax")))
                )  # fmt: skip
    officials = []
    for name, kind, title, email, phone, fax in contacts:
        (others,) = conn.execute(
            "SELECT count(*) FROM notices AS n WHERE n.agency_entity_id = :agency"
            " AND n.notice_id <> :notice_id AND ("
            " EXISTS (SELECT 1 FROM json_each(n.raw_json, '$.pointOfContact')"
            "         WHERE json_extract(value, '$.fullName') = :name)"
            " OR json_extract(n.raw_json, '$.PrimaryContactFullname') = :name"
            " OR json_extract(n.raw_json, '$.SecondaryContactFullname') = :name)",
            {"agency": agency_entity_id, "notice_id": notice_id, "name": name},
        ).fetchone()
        officials.append(Official(name, kind, title or None, email or None, phone or None,
                                  fax or None, office, others))  # fmt: skip
    return tuple(officials)


def _address(city: str | None, state: str | None, zipcode: str | None) -> str | None:
    parts = [part for part in (city, state, zipcode) if part]
    return " ".join(parts) if parts else None


def entity(conn: sqlite3.Connection, entity_id: int, *, recent: int = 10) -> EntityDetail | None:
    """One agency or office with its place in the hierarchy and its recent notices."""
    row = conn.execute(
        "SELECT entity_id, kind, name, agency_path_code, parent_entity_id, parent, notices,"
        " uei, cage FROM v_entities WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return None
    side = "vendor_entity_id" if row[1] == "contractor" else "awarding_entity_id"
    (awards_count, awards_value) = conn.execute(
        f"SELECT count(*), coalesce(sum(value_usd), 0) FROM contracts WHERE {side} = ?",
        (entity_id,),
    ).fetchone()
    won_or_made = awards(conn, **{side: entity_id}, limit=recent)
    facts = tuple(
        Fact(*item)
        for item in conn.execute(
            "SELECT predicate, value, value_type, observed_at, source_id, source_ref FROM facts"
            " WHERE subject_type = 'entity' AND subject_id = ?"
            " ORDER BY observed_at DESC, fact_id DESC",
            (str(entity_id),),
        ).fetchall()
    )
    children = tuple(
        EntityRef(*item)
        for item in conn.execute(
            "SELECT entity_id, name, agency_path_code FROM entities"
            " WHERE parent_entity_id = ? ORDER BY name",
            (entity_id,),
        ).fetchall()
    )
    aliases = tuple(
        alias
        for (alias,) in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (entity_id,)
        ).fetchall()
    )
    chain = _chain(conn, entity_id)
    parent = chain[-2] if len(chain) > 1 else None
    hits = list_notices(conn, Filters(agency_prefixes=(row[3],)), limit=recent) if row[3] else []
    return EntityDetail(
        row[0], row[1], row[2], row[3], parent, chain, children, aliases, row[6], tuple(hits),
        row[7], row[8], tuple(won_or_made), awards_count, awards_value, facts,
    )  # fmt: skip


LIST_PREDICATES = frozenset({"sam.naics", "sam.psc", "sam.business_type", "sam.sba_business_type"})


def summarize_facts(
    facts: tuple[Fact, ...], *, max_items: int | None = None
) -> list[tuple[str, str]]:
    """The latest value of each single-valued predicate and the latest set of each list
    predicate, as (predicate, text) pairs in first-seen order. ``facts`` is newest first, as
    ``EntityDetail.facts`` is; ``max_items`` truncates long lists with an ellipsis."""
    single: dict[str, str] = {}
    lists: dict[str, tuple[str, list[str]]] = {}
    for fact in facts:
        if fact.predicate in LIST_PREDICATES:
            newest, values = lists.setdefault(fact.predicate, (fact.observed_at, []))
            if fact.observed_at == newest and fact.value not in values:
                values.append(fact.value)  # two sources in one second must not double a list
        else:
            single.setdefault(fact.predicate, fact.value)
    summary = list(single.items())
    for predicate, (_, values) in lists.items():
        values.reverse()  # newest-first input lists the batch backwards
        shown = values if max_items is None or len(values) <= max_items else values[:max_items]
        more = "" if shown is values else f", … {len(values)} in all"
        summary.append((predicate, ", ".join(shown) + more))
    return summary


def recompetes(
    conn: sqlite3.Connection,
    *,
    months: int = 18,
    naics: tuple[str, ...] | None = None,
    office_code: str | None = None,
    set_aside: str | None = None,
    limit: int = 50,
) -> list[ContractRef]:
    """Awards whose period of performance (options included) ends within ``months`` from
    today, soonest first: the requirements likely to be bought again."""
    today = db.utcnow()[:10]
    rows = conn.execute(
        f"SELECT {CONTRACT_COLUMNS} FROM v_contracts"
        " WHERE coalesce(pop_potential_end, pop_end) BETWEEN :today AND date(:today, :horizon)"
        f" AND (:naics IS NULL OR {CONTRACT_NAICS_MATCH})"
        " AND (:office IS NULL OR awarding_office_code = :office)"
        " AND (:set_aside IS NULL OR set_aside_code = :set_aside)"
        " ORDER BY coalesce(pop_potential_end, pop_end), contract_id LIMIT :limit",
        {
            "today": today,
            "horizon": f"+{months} months",
            "naics": json.dumps(list(naics)) if naics else None,
            "office": office_code,
            "set_aside": set_aside,
            "limit": limit,
        },
    ).fetchall()
    return [ContractRef(*row) for row in rows]


def rank_attachments(
    conn: sqlite3.Connection, notice_ids: tuple[str, ...], text: str
) -> list[tuple[AttachmentInfo, str]]:
    """Extracted attachments of these notices, best bm25 match to ``text`` first, then the
    rest by id. Returns (attachment, notice_id) pairs."""
    if not notice_ids:
        return []
    ids = json.dumps(list(notice_ids))
    columns = (
        "a.attachment_id, a.filename, a.url, a.fetch_status, a.extract_status, a.path,"
        " length(coalesce(a.extracted_text, '')), a.notice_id"
    )
    ranked: list[tuple] = []
    tokens = [t for t in text.split() if any(c.isalnum() for c in t)]
    if tokens:
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        try:
            ranked = conn.execute(
                f"SELECT {columns} FROM attachments_fts JOIN attachments AS a"
                " ON a.attachment_id = attachments_fts.rowid"
                " WHERE attachments_fts MATCH ? AND a.extract_status = 'done'"
                " AND a.notice_id IN (SELECT value FROM json_each(?))"
                " ORDER BY bm25(attachments_fts), a.attachment_id",
                (match, ids),
            ).fetchall()
        except sqlite3.OperationalError:
            ranked = []
    seen = {row[0] for row in ranked}
    rest = conn.execute(
        f"SELECT {columns} FROM attachments AS a WHERE a.extract_status = 'done'"
        " AND a.notice_id IN (SELECT value FROM json_each(?)) ORDER BY a.attachment_id",
        (ids,),
    ).fetchall()
    rows = ranked + [row for row in rest if row[0] not in seen]
    return [(AttachmentInfo(*row[:7]), row[7]) for row in rows]


def attachment_text(conn: sqlite3.Connection, attachment_id: int) -> str | None:
    row = conn.execute(
        "SELECT extracted_text FROM attachments WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()
    return row[0] if row else None


@dataclass(frozen=True)
class RunRef:
    run_id: int
    source_id: str
    started_at: str
    finished_at: str | None
    status: str
    records_returned: int | None
    requests_spent: int
    error: str | None


def last_runs(conn: sqlite3.Connection) -> dict[str, RunRef]:
    """The latest ingestion run per source."""
    rows = conn.execute(
        "SELECT run_id, source_id, started_at, finished_at, status, records_returned,"
        " requests_spent, error FROM ingestion_runs WHERE run_id IN"
        " (SELECT max(run_id) FROM ingestion_runs GROUP BY source_id)"
    ).fetchall()
    return {row[1]: RunRef(*row) for row in rows}


def office_for_code(conn: sqlite3.Connection, code: str) -> EntityRef | None:
    """The office entity whose agency path ends with this code: one candidate, or the
    deepest of twins sharing the same department and sub-tier; otherwise None."""
    candidates = conn.execute(
        "SELECT entity_id, name, agency_path_code FROM entities WHERE kind = 'office'"
        " AND substr(agency_path_code, -length(?) - 1) = '.' || ?",
        (code, code),
    ).fetchall()
    if not candidates:
        return None
    if len(candidates) > 1:
        if len({".".join(path.split(".")[:2]) for _, _, path in candidates}) > 1:
            return None
        candidates.sort(key=lambda c: len(c[2]), reverse=True)
    return EntityRef(*candidates[0])


def contract(conn: sqlite3.Connection, contract_id: int) -> ContractRef | None:
    row = conn.execute(
        f"SELECT {CONTRACT_COLUMNS} FROM v_contracts WHERE contract_id = ?", (contract_id,)
    ).fetchone()
    return ContractRef(*row) if row else None


def notices_for_solicitation(
    conn: sqlite3.Connection, solicitation: str, *, limit: int = 10
) -> list[SearchHit]:
    """Notices carrying this solicitation number, newest first."""
    rows = conn.execute(
        "SELECT n.notice_id, n.title, e.name, n.response_deadline, n.posted_at,"
        " 'notice' AS source, coalesce(n.description, '') AS snippet, 0.0 AS rank"
        ", n.set_aside_code, s.summary, s.work_type, s.stated_set_aside"
        " FROM notices AS n LEFT JOIN entities AS e ON e.entity_id = n.agency_entity_id"
        " LEFT JOIN v_notice_summaries AS s ON s.notice_id = n.notice_id"
        " WHERE n.solicitation_number = ? ORDER BY n.posted_at DESC, n.id LIMIT ?",
        (solicitation, limit),
    ).fetchall()
    return [_hit(row, _snippet(row[6])) for row in rows]


def contractor(conn: sqlite3.Connection, uei: str, *, recent: int = 20) -> EntityDetail | None:
    """The contractor registered under this UEI, with the awards the store knows."""
    row = conn.execute(
        "SELECT entity_id FROM entities WHERE uei = ? AND kind = 'contractor'", (uei,)
    ).fetchone()
    return entity(conn, row[0], recent=recent) if row else None


def awards(
    conn: sqlite3.Connection,
    *,
    office_code: str | None = None,
    awarding_entity_id: int | None = None,
    vendor_entity_id: int | None = None,
    uei: str | None = None,
    naics: str | None = None,
    solicitation: str | None = None,
    limit: int = 20,
) -> list[ContractRef]:
    """Awards matching every given predicate, newest action first."""
    rows = conn.execute(
        f"SELECT {CONTRACT_COLUMNS} FROM v_contracts WHERE 1 = 1"
        " AND (:office_code IS NULL OR awarding_office_code = :office_code)"
        " AND (:awarding_entity_id IS NULL OR awarding_entity_id = :awarding_entity_id)"
        " AND (:vendor_entity_id IS NULL OR vendor_entity_id = :vendor_entity_id)"
        " AND (:uei IS NULL OR vendor_uei = :uei)"
        " AND (:naics IS NULL OR naics_code = :naics)"
        " AND (:solicitation IS NULL OR solicitation_identifier = :solicitation)"
        " ORDER BY last_action_date DESC, contract_id DESC LIMIT :limit",
        {
            "office_code": office_code,
            "awarding_entity_id": awarding_entity_id,
            "vendor_entity_id": vendor_entity_id,
            "uei": uei,
            "naics": naics,
            "solicitation": solicitation,
            "limit": limit,
        },  # fmt: skip
    ).fetchall()
    return [ContractRef(*row) for row in rows]


def upcoming(conn: sqlite3.Connection, *, days: int = 7, limit: int = 50) -> list[SearchHit]:
    """Active notices due within ``days``, soonest first."""
    return list_notices(conn, Filters(deadline_within_days=days, active_only=True), limit=limit)


def activity(conn: sqlite3.Connection, *, days: int = 14) -> list[tuple[str, int]]:
    """Notices first seen per UTC day, one entry per day ending today, oldest first."""
    return _daily(
        conn,
        "SELECT date(first_seen_at) AS day, count(*) FROM notices"
        " WHERE date(first_seen_at) >= :start GROUP BY day",
        days,
    )


def quota_history(conn: sqlite3.Connection, *, days: int = 30) -> list[tuple[str, int]]:
    """Keyed SAM.gov requests per UTC day, one entry per day ending today, oldest first."""
    return _daily(conn, "SELECT day, requests FROM v_quota_daily WHERE day >= :start", days)


def counts(conn: sqlite3.Connection) -> StoreCounts:
    (notices, active) = conn.execute("SELECT count(*), sum(active) FROM notices").fetchone()
    (entities,) = conn.execute("SELECT count(*) FROM entities").fetchone()
    return StoreCounts(notices, active or 0, entities)


def _chain(conn: sqlite3.Connection, entity_id: int | None) -> tuple[EntityRef, ...]:
    if entity_id is None:
        return ()
    return tuple(EntityRef(*row) for row in conn.execute(CHAIN, {"id": entity_id}).fetchall())


def _daily(conn: sqlite3.Connection, sql: str, days: int) -> list[tuple[str, int]]:
    today = datetime.strptime(db.utcnow(), db.TIMESTAMP_FORMAT).date()
    start = today - timedelta(days=days - 1)
    found = dict(conn.execute(sql, {"start": start.isoformat()}).fetchall())
    series = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).isoformat()
        series.append((day, found.get(day, 0)))
    return series
