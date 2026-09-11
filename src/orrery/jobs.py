"""The operations a user runs: one registry for the CLI and the terminal UI.

Each job names what it needs (a SAM.gov key, NAICS codes), the parameters it takes, whether
it can be cancelled between units of work, and how to run it against a connection and
settings with progress and cancellation callbacks (``orrery.progress``). ``run`` validates
parameters, maps every adapter error to ``JobFailed`` with the message the CLI has always
printed, and lets ``JobCancelled`` through. ``summarize`` renders a result the way the CLI
prints it. Serves docs/DESIGN.md §4 (surfaces) and §9.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from orrery import assess, db, naics, query, summaries, workspace
from orrery.ai import AIError
from orrery.config import Settings
from orrery.embed.client import EmbeddingError
from orrery.embed.pipeline import embed_pending
from orrery.extract.text import extract_pending
from orrery.fetch import queue
from orrery.ingest import awards, bulk, entities, notices
from orrery.progress import Cancelled, JobCancelled, Report, check, never, quiet
from orrery.quota import BudgetExceeded
from orrery.sam.client import SamError
from orrery.usaspending.client import UsaspendingError

Runner = Callable[[sqlite3.Connection, Settings, dict, Report, Cancelled], object]


class JobFailed(Exception):
    """The job could not run or stopped; the message is safe to show (keys are redacted by
    the adapters). ``exit_code`` 2 marks a usage or configuration problem, 1 a failure."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    kind: Literal["date", "int", "str", "csv", "choice", "bool"]
    default: object = None
    choices: tuple[str, ...] = ()
    help: str = ""
    required: bool = False


@dataclass(frozen=True)
class Job:
    name: str
    label: str
    needs: frozenset[str]
    """Static preconditions: 'sam_key', 'naics'."""
    params: tuple[Param, ...]
    cancellable: bool
    run: Runner
    unmet: Callable[[Settings, dict], list[str]] | None = None
    """Conditional preconditions beyond ``needs``."""


def needs_unmet(job: Job, settings: Settings, params: dict) -> list[str]:
    """Why the job cannot run yet, in the words the CLI uses; empty when it can."""
    problems = []
    if "sam_key" in job.needs and settings.sam_api_key is None:
        problems.append("ORRERY_SAM_API_KEY is not set")
    if "naics" in job.needs and not settings.naics:
        problems.append("ORRERY_NAICS is empty; nothing to ingest")
    if job.unmet is not None:
        problems += job.unmet(settings, params)
    return problems


def run(
    job: Job,
    conn: sqlite3.Connection,
    settings: Settings,
    params: dict,
    *,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> object:
    """Run the job. Raises ``JobFailed`` for usage and adapter errors, ``JobCancelled`` when
    the user stopped it; the run rows in the store are closed either way."""
    values = coerce(job, params)
    if "naics" in job.needs and (warning := naics.vintage_warning()):
        report(warning)
    try:
        return job.run(conn, settings, values, report, cancelled)
    except JobCancelled:
        raise
    except JobFailed:
        raise
    except (BudgetExceeded, SamError, UsaspendingError, EmbeddingError, AIError) as exc:
        raise JobFailed(f"{_prefix(job)}{exc}") from exc
    except (bulk.BulkError, awards.AwardsError, entities.EntitiesError) as exc:
        raise JobFailed(f"{_prefix(job)}{exc}") from exc
    except (ValueError, workspace.NotFound) as exc:
        raise JobFailed(f"{_prefix(job)}{exc}") from exc


MANIFESTS_PER_RUN = 200
"""Notices asked about per fetch run, unless the caller says otherwise.

Bounded rather than unlimited, unlike the other two limits. Steady state is a few dozen checks
a day, but a first run on a backfilled store faces every notice in it at once, against an
endpoint whose rate limits are not published (docs/notes/sam-manifest-probe.md). Nothing is
lost by stopping early: unchecked notices stay due and the next run continues.
"""

PREFIXES = {
    "ingest-notices": "ingestion stopped: ",
    "ingest-bulk": "bulk ingest stopped: ",
    "ingest-awards": "awards ingest stopped: ",
    "ingest-entities": "entities ingest stopped: ",
    "embed": "embedding stopped: ",
    "summarize": "summarize stopped: ",
    "sync": "sync stopped: ",
}


def _prefix(job: Job) -> str:
    return PREFIXES.get(job.name, "")


def coerce(job: Job, params: dict) -> dict:
    """Parameters as the runners expect them: dates, ints, bools, lists; strings may come
    from a form. Missing ones take their defaults; a bad value is a usage error."""
    values: dict = {}
    for param in job.params:
        raw = params.get(param.name, param.default)
        if raw in (None, ""):
            if param.required:
                raise JobFailed(f"{param.label} is required", exit_code=2)
            values[param.name] = None if param.kind != "bool" else bool(param.default)
            continue
        try:
            values[param.name] = _coerce_one(param, raw)
        except (ValueError, TypeError) as exc:
            raise JobFailed(f"{param.label}: {exc}", exit_code=2) from exc
    return values


def _coerce_one(param: Param, raw: object) -> object:
    if param.kind == "date":
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
        return date.fromisoformat(str(raw).strip())
    if param.kind == "int":
        return int(raw)
    if param.kind == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    if param.kind == "csv":
        if isinstance(raw, str):
            return [v.strip() for v in raw.split(",") if v.strip()]
        return list(raw)
    if param.kind == "choice":
        value = str(raw).strip()
        if value not in param.choices:
            raise ValueError(f"must be one of {', '.join(param.choices)}")
        return value
    return str(raw)


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def default_window(job_name: str, today: date | None = None) -> tuple[date, date]:
    """The date window a job uses when none is given: yesterday for notices, three years
    for awards."""
    today = today or datetime.now(UTC).date()
    if job_name == "ingest-awards":
        return years_before(today, 3), today
    return date.fromordinal(today.toordinal() - 1), today


# Runners ------------------------------------------------------------------------------------


def _window(job_name: str, values: dict) -> tuple[date, date]:
    since, until = default_window(job_name)
    posted_from = values.get("since") or since
    posted_to = values.get("until") or until
    if posted_from > posted_to:
        raise JobFailed("--since must not be after --until", exit_code=2)
    return posted_from, posted_to


def _run_notices(conn, settings, values, report, cancelled) -> object:
    posted_from, posted_to = _window("ingest-notices", values)
    return notices.ingest_notices(
        conn, settings, posted_from=posted_from, posted_to=posted_to,
        report=report, cancelled=cancelled,
    )  # fmt: skip


def _run_bulk(conn, settings, values, report, cancelled) -> object:
    archived, file = values.get("archived"), values.get("file")
    if archived is not None and file is not None:
        raise JobFailed("--archived and --file are mutually exclusive", exit_code=2)
    extract = (
        bulk.Extract(Path(file))
        if file
        else bulk.fetch_extract(settings.data_dir / "extracts", fiscal_year=archived, report=report)
    )
    report(f"extract: {extract.path}")
    return bulk.ingest_bulk(
        conn, settings, extract.path, mark_inactive=(file is None and archived is None),
        limit=values.get("limit"), generated_at=extract.generated_at,
        report=report, cancelled=cancelled,
    )  # fmt: skip


SYNC_AI_STAGES = ("summarize", "embed")
"""The stages that reach a model. ``--no-ai`` drops exactly these, so an install with none
configured still runs the loop that needs no model."""


@dataclass(frozen=True)
class SyncResult:
    """What each stage of the standard loop did, in the order they ran.

    A stage is None when it did not run, and ``skipped`` says which were dropped on purpose,
    so a loop that did less than the whole is never read as a loop that found nothing to do.
    The daily budget is not a failure: ``fetch.budget_exhausted`` reports it and the stages
    after it, which spend no quota, run anyway.
    """

    ingest_bulk: object | None = None
    fetch: object | None = None
    extract: object | None = None
    summarize: object | None = None
    embed: object | None = None
    skipped: tuple[str, ...] = ()


def _run_sync(conn, settings, values, report, cancelled) -> object:
    """Run the standard loop in order, one stage at a time on this connection.

    Every stage is individually resumable and idempotent, so a sync that stops part way is
    continued by the next one. A stage that fails stops the loop there: the stages before it
    have committed their work, and the failure is the CLI's exit code rather than a line
    buried in a summary.
    """
    limit = values.get("limit")
    skipped = SYNC_AI_STAGES if values.get("no_ai") else ()
    done: dict[str, object] = {}

    report("sync: ingest bulk")
    extract = bulk.fetch_extract(settings.data_dir / "extracts", report=report)
    report(f"extract: {extract.path}")
    done["ingest_bulk"] = bulk.ingest_bulk(
        conn, settings, extract.path, mark_inactive=True, limit=limit,
        generated_at=extract.generated_at, report=report, cancelled=cancelled,
    )  # fmt: skip
    check(cancelled)

    report("sync: fetch")
    fetched = queue.fetch_pending(
        conn, settings, budget=limit, max_attachments=limit,
        max_manifests=limit if limit is not None else MANIFESTS_PER_RUN,
        report=report, cancelled=cancelled,
    )  # fmt: skip
    done["fetch"] = fetched
    if fetched.budget_exhausted:
        report("sync: today's SAM.gov budget is spent; descriptions resume tomorrow")
    check(cancelled)

    report("sync: extract")
    done["extract"] = extract_pending(
        conn, settings, limit=limit, report=report, cancelled=cancelled
    )
    check(cancelled)

    if "summarize" not in skipped:
        report("sync: summarize")
        done["summarize"] = summaries.summarize_pending(
            conn, settings, slot="fast", limit=limit, report=report, cancelled=cancelled
        )
        check(cancelled)
    if "embed" not in skipped:
        report("sync: embed")
        done["embed"] = embed_pending(
            conn, settings, limit=limit, report=report, cancelled=cancelled
        )

    return SyncResult(**done, skipped=skipped)


def _run_awards(conn, settings, values, report, cancelled) -> object:
    file = values.get("file")
    if file is not None and (values.get("since") or values.get("until")):
        raise JobFailed("--file ignores the window; drop --since/--until", exit_code=2)
    posted_from, posted_to = _window("ingest-awards", values)

    def sleep(seconds: float) -> None:
        check(cancelled)
        import time

        time.sleep(seconds)

    path = (
        Path(file)
        if file
        else awards.fetch_awards(
            settings, since=posted_from, until=posted_to, sleep=sleep, report=report
        )
    )
    report(f"awards: {path}")
    return awards.ingest_awards(
        conn,
        settings,
        path,
        limit=values.get("limit"),
        since=None if file else posted_from,
        until=None if file else posted_to,
        report=report,
        cancelled=cancelled,
    )


def _entities_unmet(settings: Settings, params: dict) -> list[str]:
    values = coerce(JOBS["ingest-entities"], params)
    extract_dir = settings.data_dir / "extracts" / "sam"
    needs_key = values.get("uei") or (
        values.get("file") is None
        and (values.get("refresh") or entities.newest_extract(extract_dir) is None)
    )
    return ["ORRERY_SAM_API_KEY is not set"] if needs_key and settings.sam_api_key is None else []


def _run_entities(conn, settings, values, report, cancelled) -> object:
    ueis, file = values.get("uei"), values.get("file")
    if ueis and file is not None:
        raise JobFailed("--uei and --file are mutually exclusive", exit_code=2)
    if ueis:
        return entities.lookup_entities(conn, settings, ueis, report=report, cancelled=cancelled)
    extract_dir = settings.data_dir / "extracts" / "sam"
    path = Path(file) if file else None
    if path is None and not values.get("refresh"):
        path = entities.newest_extract(extract_dir)
    if path is None:
        path = entities.fetch_extract(conn, settings, extract_dir, report=report)
    report(f"extract: {path}")
    return entities.ingest_extract(
        conn, settings, path, limit=values.get("limit"), report=report, cancelled=cancelled
    )


def _run_fetch(conn, settings, values, report, cancelled) -> object:
    return queue.fetch_pending(
        conn, settings, budget=values.get("budget"),
        max_attachments=values.get("max_attachments"),
        max_manifests=values.get("max_manifests"),
        report=report, cancelled=cancelled,
    )  # fmt: skip


def _run_extract(conn, settings, values, report, cancelled) -> object:
    return extract_pending(
        conn, settings, limit=values.get("limit"), report=report, cancelled=cancelled
    )


def _run_embed(conn, settings, values, report, cancelled) -> object:
    return embed_pending(
        conn, settings, limit=values.get("limit"), report=report, cancelled=cancelled
    )


def _run_summarize(conn, settings, values, report, cancelled) -> object:
    return summaries.summarize_pending(
        conn,
        settings,
        slot=values["slot"],
        limit=values.get("limit"),
        report=report,
        cancelled=cancelled,
    )


def _run_assess(conn, settings, values, report, cancelled) -> object:
    return assess.assess(conn, settings, values["pursuit_id"], slot=values["slot"], warn=report)


def _run_migrate(conn, settings, values, report, cancelled) -> object:
    applied = db.migrate(conn)
    for name in applied:
        report(f"applied {name}")
    return applied


def _run_reindex(conn, settings, values, report, cancelled) -> object:
    query.rebuild_search(conn)
    return None


# Connection probes: the cheapest request that proves a setting works.


def _probe_sam(conn, settings, values, report, cancelled) -> object:
    from datetime import timedelta

    from orrery import runs
    from orrery.sam.client import SamClient

    today = datetime.now(UTC).date()
    run_id = runs.start(conn, "sam_opportunities_api")
    try:
        with SamClient(settings, conn, run_id) as client:
            page = next(
                client.search_pages(today - timedelta(days=1), today, settings.naics[0], limit=1),
                None,
            )
    except Exception as exc:
        runs.finish(conn, run_id, status="failed", error=str(exc))
        raise
    runs.finish(conn, run_id, status="succeeded", records_returned=0)
    total = page.total_records if page else 0
    return (
        f"SAM.gov ok · {total} notices posted yesterday for {settings.naics[0]} · 1 request spent"
    )


def _probe_embed(conn, settings, values, report, cancelled) -> object:
    from orrery.embed.client import EmbeddingClient

    with EmbeddingClient(settings) as client:
        [vector] = client.embed(["orrery"])
    return f"embeddings ok · {settings.embed_model} · {len(vector)} dimensions"


def _probe_slot(name: str) -> Runner:
    def run(conn, settings, values, report, cancelled) -> object:
        from orrery import ai

        slot = ai.resolve_slot(settings, name)
        backend = ai.backend_for(slot)
        try:
            completion = backend.complete(
                system="Answer with one JSON object.",
                user='Return {"ok": true}.',
                schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                max_tokens=64,
            )
        finally:
            backend.close()
        tokens = (
            f"{completion.input_tokens} in / {completion.output_tokens} out"
            if completion.input_tokens is not None
            else "tokens unknown"
        )
        return f"{slot.name} ok · {slot.provider} {slot.model} · {tokens}"

    return run


JOBS: dict[str, Job] = {
    job.name: job
    for job in (
        Job(
            "ingest-notices", "Ingest notices (SAM.gov API)", frozenset({"sam_key", "naics"}),
            (Param("since", "Posted since", "date", help="YYYY-MM-DD; default yesterday"),
             Param("until", "Posted until", "date", help="YYYY-MM-DD; default today")),
            True, _run_notices,
        ),
        Job(
            "ingest-bulk", "Ingest bulk extract (SAM.gov, no key)", frozenset({"naics"}),
            (Param("archived", "Archived fiscal year", "int", help="e.g. 2025; blank: active"),
             Param("file", "Local extract file", "str"),
             Param("limit", "Row limit", "int")),
            True, _run_bulk,
        ),
        Job(
            "ingest-awards", "Ingest awards (USAspending, no key)", frozenset({"naics"}),
            (Param("since", "Actions since", "date", help="default three years ago"),
             Param("until", "Actions until", "date", help="default today"),
             Param("file", "Local award file", "str"),
             Param("limit", "Row limit", "int")),
            True, _run_awards,
        ),
        Job(
            "ingest-entities", "Ingest entity registrations (SAM.gov)", frozenset(),
            (Param("uei", "UEIs to look up", "csv", help="comma-separated; one request per ten"),
             Param("file", "Local extract file", "str"),
             Param("refresh", "Download this month's extract", "bool", default=False),
             Param("limit", "Registrant limit", "int")),
            True, _run_entities, unmet=_entities_unmet,
        ),
        Job(
            "fetch", "Fetch descriptions and attachments", frozenset(),
            (Param("budget", "Description budget", "int"),
             Param("max_attachments", "Attachment limit", "int"),
             Param("max_manifests", "Discovery limit", "int", default=MANIFESTS_PER_RUN)),
            True, _run_fetch,
        ),
        Job("extract", "Extract attachment text", frozenset(),
            (Param("limit", "Attachment limit", "int"),), True, _run_extract),
        Job("embed", "Embed text", frozenset(),
            (Param("limit", "Source limit", "int"),), True, _run_embed),
        Job(
            "summarize", "Summarize notices", frozenset(),
            (Param("limit", "Notice limit", "int"),
             Param("slot", "Model slot", "choice", default="fast", choices=("fast", "deep"))),
            True, _run_summarize,
        ),
        Job(
            "sync", "Run the standard loop", frozenset({"naics"}),
            (Param("no_ai", "Skip the model stages", "bool", default=False),
             Param("limit", "Per-stage limit", "int")),
            True, _run_sync,
        ),
        Job(
            "assess", "Assess a pursuit", frozenset(),
            (Param("pursuit_id", "Pursuit id", "int", required=True),
             Param("slot", "Model slot", "choice", default="deep", choices=("deep", "fast"))),
            False, _run_assess,
        ),
        Job("db-migrate", "Apply schema migrations", frozenset(), (), False, _run_migrate),
        Job("db-reindex", "Rebuild the search indexes", frozenset(), (), False, _run_reindex),
        Job("probe-sam", "Test the SAM.gov key (one request)", frozenset({"sam_key", "naics"}),
            (), False, _probe_sam),
        Job("probe-embed", "Test the embeddings endpoint", frozenset(), (), False, _probe_embed),
        Job("probe-fast", "Test the fast model", frozenset(), (), False, _probe_slot("fast")),
        Job("probe-deep", "Test the deep model", frozenset(), (), False, _probe_slot("deep")),
    )
}  # fmt: skip

OPERATIONS = tuple(name for name in JOBS if not name.startswith("probe-"))
"""The jobs the Jobs tab lists; probes run from the Connections tab."""


def summarize(job: Job, result: object) -> str:
    """The one-line (or few-line) text the CLI prints for a result."""
    r = result
    match job.name:
        case "ingest-notices":
            return (
                f"run {r.run_id}: {r.notices_seen} notices seen, {r.notices_new} new,"
                f" {r.versions_added} versions, {r.attachments_added} attachments,"
                f" {r.requests_spent} requests"
            )
        case "ingest-bulk":
            resumed = f" (resumed at row {r.resumed_from})" if r.resumed_from else ""
            inactive = (
                f"{r.notices_deactivated} marked inactive"
                if r.active_pass == bulk.ACTIVE_PASS_DONE
                else f"active pass {r.active_pass}"
            )
            return (
                f"run {r.run_id}: {r.rows_read} rows read, {r.rows_matched} in slice,"
                f" {r.notices_new} new, {r.notices_updated} updated,"
                f" {r.descriptions_filled} descriptions filled, {r.versions_added} versions,"
                f" {inactive}{resumed}"
            )
        case "ingest-awards":
            resumed = f" (resumed at row {r.resumed_from})" if r.resumed_from else ""
            return (
                f"run {r.run_id}: {r.rows_read} rows read, {r.rows_matched} in slice,"
                f" {r.contracts_new} new, {r.contracts_updated} updated,"
                f" {r.contractors_new} contractors new, {r.offices_unresolved} offices and"
                f" {r.vendors_unresolved} vendors unresolved{resumed}"
            )
        case "ingest-entities":
            resumed = f" (resumed at row {r.resumed_from})" if r.resumed_from else ""
            return (
                f"run {r.run_id}: {r.rows_read} registrants read, {r.rows_matched} in slice,"
                f" {r.rows_malformed} malformed, {r.entities_new} contractors new,"
                f" {r.registrations_added} registrations, {r.facts_added} facts,"
                f" {r.requests_spent} requests{resumed}"
            )
        case "fetch":
            note = (
                " (daily budget exhausted; attachments still fetched)" if r.budget_exhausted else ""
            )
            if r.failures:
                # Which kinds, not just how many: 404 is permanent, a 429 or a 5xx is worth
                # another run, and the counts alone cannot tell them apart.
                note += " [" + ", ".join(f"{k} {n}" for k, n in r.failures.items()) + "]"
            return (
                f"run {r.run_id}: {r.descriptions_fetched} descriptions fetched,"
                f" {r.descriptions_failed} failed; {r.manifests_checked} manifests read,"
                f" {r.manifests_failed} failed, {r.attachments_found} attachments found;"
                f" {r.attachments_fetched} attachments"
                f" fetched, {r.attachments_failed} failed, {r.attachments_skipped} skipped;"
                f" {r.requests_spent} requests{note}"
            )
        case "extract":
            return f"{r.done} extracted, {r.unsupported} unsupported, {r.failed} failed"
        case "embed":
            return (
                f"{r.notices} notices, {r.attachments} attachments,"
                f" {r.chunks} chunks embedded with {r.model}"
            )
        case "summarize":
            return f"{r.summarized} notices summarized, {r.failed} invalid, with {r.model}"
        case "sync":
            # Each stage says what it did in its own words, so the loop has no second
            # vocabulary to keep in step with the commands it runs.
            lines = [
                f"{stage}: {summarize(JOBS[stage], result)}"
                for stage, result in (
                    ("ingest-bulk", r.ingest_bulk), ("fetch", r.fetch), ("extract", r.extract),
                    ("summarize", r.summarize), ("embed", r.embed),
                )
                if result is not None
            ]  # fmt: skip
            if r.skipped:
                lines.append(f"skipped: {', '.join(r.skipped)}")
            return "\n".join(lines)
        case "assess":
            return "\n".join(assess.describe(r))
        case "db-migrate":
            return "up to date" if not r else "\n".join(f"applied {name}" for name in r)
        case "db-reindex":
            return "search index rebuilt"
        case name if name.startswith("probe-"):
            return str(r)
    return str(r)
