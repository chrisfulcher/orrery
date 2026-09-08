"""Command-line entry point."""

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import click
import typer

from mentor import __version__, assess, db, documents, jobs, query, workspace
from mentor import quota as quota_module
from mentor.ai import AIError
from mentor.config import Settings
from mentor.documents import DocumentError, ProfileDocument, SearchDocument, WorkflowDocument
from mentor.embed.client import EmbeddingClient, EmbeddingError, pack
from mentor.fetch import queue

app = typer.Typer(
    no_args_is_help=True,
    help="Self-hosted business development intelligence for U.S. federal contracting.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database schema and index maintenance.")
app.add_typer(db_app, name="db")
ingest_app = typer.Typer(no_args_is_help=True, help="Pull SAM.gov data into the local store.")
app.add_typer(ingest_app, name="ingest")
searches_app = typer.Typer(no_args_is_help=True, help="Saved searches.")
app.add_typer(searches_app, name="searches")
profile_app = typer.Typer(no_args_is_help=True, help="Your company profile.")
app.add_typer(profile_app, name="profile")
workflow_app = typer.Typer(no_args_is_help=True, help="Your stages, gates, and task templates.")
app.add_typer(workflow_app, name="workflow")
pursuit_app = typer.Typer(no_args_is_help=True, help="One pursuit: create, link, decide, work.")
app.add_typer(pursuit_app, name="pursuit")

JsonFlag = Annotated[bool, typer.Option("--json", help="Print as JSON.")]
DateOption = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], metavar="YYYY-MM-DD")]


def print_json(payload: object) -> None:
    """Every ``--json`` output goes through here: one document on stdout."""
    typer.echo(json.dumps(payload, indent=2, default=str))


MILESTONES = {
    "ingest-bulk": ("extract: ",),
    "ingest-awards": ("awards: ",),
    "ingest-entities": ("extract: ",),
    "assess": ("",),
}
"""The report lines each command has always printed to stderr; the rest is progress the
app's log shows."""


def _run_job(
    name: str, params: dict, json_output: bool = False, *, heading: str | None = None
) -> object:
    """Run a registry job the way the CLI always has: preconditions exit 2, failures exit
    with the job's code, the result is printed as JSON or as its one-line summary."""
    settings = Settings()
    job = jobs.JOBS[name]
    prefixes = MILESTONES.get(name, ())

    def report(message: str) -> None:
        if message.startswith(prefixes):
            typer.echo(message, err=True)

    try:
        unmet = jobs.needs_unmet(job, settings, params)
        if unmet:
            typer.echo(unmet[0], err=True)
            raise typer.Exit(2)
        with closing(db.connect(settings.db_path)) as conn:
            result = jobs.run(job, conn, settings, params, report=report)
    except jobs.JobFailed as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(exc.exit_code) from exc
    if json_output:
        print_json(dataclasses.asdict(result))
    else:
        if heading:
            typer.echo(heading)
        typer.echo(jobs.summarize(job, result))
    return result


@app.callback()
def main() -> None:
    """Group callback; makes every command a subcommand."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"mentor {__version__}")


@app.command()
def quota(json_output: JsonFlag = False) -> None:
    """Show today's SAM.gov request budget (UTC day)."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        spent = quota_module.spent_today(conn)
    budget = settings.sam_daily_budget
    remaining = max(budget - spent, 0)
    if json_output:
        print_json(
            {
                "date": datetime.now(UTC).date().isoformat(),
                "spent": spent,
                "budget": budget,
                "remaining": remaining,
            }
        )
    else:
        typer.echo(f"spent {spent} of {budget} today (UTC), {remaining} remaining")


@app.command()
def fetch(
    budget: Annotated[int | None, typer.Option(help="Cap descriptions fetched this run.")] = None,
    max_attachments: Annotated[
        int | None, typer.Option(help="Cap attachment downloads this run.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the queue; fetch nothing.")
    ] = False,
    json_output: JsonFlag = False,
) -> None:
    """Fetch pending notice descriptions (within today's budget) and attachments (free)."""
    settings = Settings()
    if dry_run:
        with closing(db.connect(settings.db_path)) as conn:
            status = queue.queue_status(conn)
            remaining = quota_module.remaining(conn, settings)
        if json_output:
            print_json({**dataclasses.asdict(status), "budget_remaining": remaining})
        else:
            typer.echo(
                f"pending: {status.descriptions_pending} descriptions,"
                f" {status.attachments_pending} attachments;"
                f" {remaining} requests remaining today"
            )
            for item in status.next_descriptions:
                deadline = item.response_deadline or "-"
                typer.echo(f"  {item.notice_id}  {deadline:20}  {item.title}")
        return
    _run_job("fetch", {"budget": budget, "max_attachments": max_attachments}, json_output)


@app.command()
def extract(
    limit: Annotated[int | None, typer.Option(help="Cap attachments processed this run.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Extract text from fetched attachments (PDF for now). Spends no quota."""
    _run_job("extract", {"limit": limit}, json_output)


@app.command()
def embed(
    limit: Annotated[int | None, typer.Option(help="Cap sources embedded this run.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Embed fetched descriptions and extracted attachment text via the configured endpoint."""
    _run_job("embed", {"limit": limit}, json_output)


@app.command()
def summarize(
    limit: Annotated[int | None, typer.Option(help="Cap notices summarized this run.")] = None,
    slot: Annotated[str, typer.Option("--slot", help="fast (the default) or deep.")] = "fast",
    json_output: JsonFlag = False,
) -> None:
    """Summarize and tag fetched notice descriptions with the configured model; stored with
    provenance. Spends no quota."""
    _run_job("summarize", {"limit": limit, "slot": slot}, json_output)


NaicsOption = Annotated[str | None, typer.Option("--naics", help="NAICS codes, comma-separated.")]
SetAsideOption = Annotated[
    str | None, typer.Option("--set-aside", help="Set-aside codes, comma-separated.")
]
AgencyOption = Annotated[
    str | None, typer.Option("--agency", help="Agency path code prefixes, comma-separated.")
]
DeadlineOption = Annotated[
    int | None, typer.Option("--deadline-days", help="Only deadlines within N days.")
]


def _csv(value: str | None) -> tuple[str, ...] | None:
    items = tuple(item.strip() for item in (value or "").split(",") if item.strip())
    return items or None


def _filters(
    naics: str | None, set_aside: str | None, agency: str | None, deadline_days: int | None
) -> query.Filters:
    return query.Filters(
        naics=_csv(naics),
        set_asides=_csv(set_aside),
        agency_prefixes=_csv(agency),
        deadline_within_days=deadline_days,
    )


def _print_hits(hits: list[query.SearchHit], json_output: bool) -> None:
    if json_output:
        print_json([dataclasses.asdict(hit) for hit in hits])
        return
    if not hits:
        typer.echo("no matches")
        return
    for index, hit in enumerate(hits):
        if index:
            typer.echo("")
        typer.echo(f"{hit.notice_id}  {hit.title}")
        typer.echo(f"  {hit.agency or '-'}  deadline {hit.response_deadline or '-'}")
        page = f" p.{hit.page}" if hit.page else ""
        typer.echo(f"  {hit.source}{page}: {hit.snippet}")


@app.command()
def search(
    text: Annotated[str, typer.Argument(metavar="QUERY", help="Words, phrases, or FTS5 syntax.")],
    limit: Annotated[int, typer.Option(help="Maximum notices to show.")] = 20,
    semantic: Annotated[
        bool, typer.Option("--semantic", help="Rank by meaning via the embedding endpoint.")
    ] = False,
    naics: NaicsOption = None,
    set_aside: SetAsideOption = None,
    agency: AgencyOption = None,
    deadline_days: DeadlineOption = None,
    json_output: JsonFlag = False,
) -> None:
    """Search notice text and attachment text; one best hit per notice."""
    settings = Settings()
    filters = _filters(naics, set_aside, agency, deadline_days)
    if semantic and filters != query.NO_FILTERS:
        typer.echo("filters apply to keyword search only", err=True)
        raise typer.Exit(2)
    with closing(db.connect(settings.db_path)) as conn:
        if semantic:
            hits = _semantic_hits(conn, settings, text, limit)
        else:
            try:
                hits = query.search(conn, text, limit=limit, filters=filters)
            except query.InvalidQuery as exc:
                typer.echo(f"invalid query: {exc}", err=True)
                raise typer.Exit(1) from exc
    _print_hits(hits, json_output)


def _semantic_hits(conn, settings: Settings, text: str, limit: int) -> list[query.SearchHit]:
    try:
        db.load_vec(conn)
    except db.VecUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    try:
        with EmbeddingClient(settings) as client:
            [vector] = client.embed([text])
    except EmbeddingError as exc:
        typer.echo(f"search stopped: {exc}", err=True)
        raise typer.Exit(1) from exc
    return query.semantic_search(conn, pack(vector), model=settings.embed_model, limit=limit)


def _money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "-"


def _print_contracts(rows: list[query.ContractRef], json_output: bool) -> None:
    if json_output:
        print_json([dataclasses.asdict(row) for row in rows])
        return
    if not rows:
        typer.echo("no awards")
    for row in rows:
        typer.echo(
            f"{row.last_action_date or '-'}  {row.piid:<20} {_money(row.value_usd):>15}"
            f"  {row.set_aside_code or '-':<8} {row.vendor or '-'}  @ {row.awarding_office or '-'}"
        )


@app.command("awards")
def awards_command(
    office: Annotated[
        str | None, typer.Option("--office", help="Awarding office code (the AAC).")
    ] = None,
    uei: Annotated[str | None, typer.Option("--uei", help="Vendor UEI.")] = None,
    naics: Annotated[str | None, typer.Option("--naics", help="One NAICS code.")] = None,
    solicitation: Annotated[
        str | None, typer.Option("--solicitation", help="Solicitation identifier.")
    ] = None,
    limit: Annotated[int, typer.Option(help="Maximum awards to show.")] = 20,
    json_output: JsonFlag = False,
) -> None:
    """Award history from USAspending, newest action first."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        rows = query.awards(
            conn, office_code=office, uei=uei, naics=naics, solicitation=solicitation, limit=limit
        )
    _print_contracts(rows, json_output)


@app.command("recompetes")
def recompetes_command(
    months: Annotated[int, typer.Option(help="How far ahead to look.")] = 18,
    naics: Annotated[
        str | None,
        typer.Option("--naics", help="Comma-separated; default: the profile's offerings."),
    ] = None,
    office: Annotated[str | None, typer.Option("--office", help="Awarding office code.")] = None,
    set_aside: Annotated[str | None, typer.Option("--set-aside")] = None,
    limit: Annotated[int, typer.Option(help="Maximum awards to show.")] = 50,
    json_output: JsonFlag = False,
) -> None:
    """The recompete radar: awards ending soonest, options included, likely to be bought again."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        codes = _csv(naics)
        if codes is None:
            profile = workspace.get_profile(conn)
            codes = profile.naics if profile and profile.naics else None
        rows = query.recompetes(
            conn, months=months, naics=codes, office_code=office, set_aside=set_aside, limit=limit
        )
    if json_output:
        print_json([dataclasses.asdict(row) for row in rows])
        return
    if not rows:
        typer.echo("no awards end in the window")
    for row in rows:
        typer.echo(
            f"{row.pop_potential_end or row.pop_end}  {row.piid:<20} {_money(row.value_usd):>15}"
            f"  {row.set_aside_code or '-':<8} {row.vendor or '-'}  @ {row.awarding_office or '-'}"
            f"  [{row.contract_id}]"
        )


@app.command("contractor")
def contractor_command(
    uei: Annotated[str, typer.Argument(metavar="UEI")],
    limit: Annotated[int, typer.Option(help="Maximum awards to show.")] = 20,
    json_output: JsonFlag = False,
) -> None:
    """One contractor by UEI: names, awards won, and what the store knows about it."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        detail = query.contractor(conn, uei, recent=limit)
    if detail is None:
        typer.echo(f"no contractor {uei}", err=True)
        raise typer.Exit(1)
    if json_output:
        print_json(dataclasses.asdict(detail))
        return
    typer.echo(f"{detail.name}  uei {detail.uei}  cage {detail.cage or '-'}")
    typer.echo(f"also seen as: {', '.join(a for a in detail.aliases if a != detail.name) or '-'}")
    typer.echo(f"awards: {detail.awards_count}, {_money(detail.awards_value_usd)} current value")
    for predicate, value in query.summarize_facts(detail.facts):
        typer.echo(f"  {predicate}: {value}")
    if detail.facts:
        typer.echo(f"  ({detail.facts[0].source_id}, observed {detail.facts[0].observed_at})")
    _print_contracts(list(detail.awards), False)


@searches_app.command("add")
def searches_add(
    name: Annotated[str, typer.Argument(help="A name unique to you; re-adding replaces it.")],
    query_text: Annotated[str | None, typer.Option("--query", help="FTS5 text.")] = None,
    naics: NaicsOption = None,
    set_aside: SetAsideOption = None,
    agency: AgencyOption = None,
    deadline_days: DeadlineOption = None,
) -> None:
    """Save a named search: optional text plus filters."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        saved = workspace.save_search(
            conn,
            name,
            query_text=query_text,
            filters=_filters(naics, set_aside, agency, deadline_days),
        )
    typer.echo(f"saved {saved.name}")


@searches_app.command("list")
def searches_list(json_output: JsonFlag = False) -> None:
    """List saved searches."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        searches = workspace.list_searches(conn)
    if json_output:
        print_json([dataclasses.asdict(saved) for saved in searches])
        return
    if not searches:
        typer.echo("no saved searches")
    for saved in searches:
        typer.echo(f"{saved.name}  {workspace.describe_search(saved)}")


@searches_app.command("edit")
def searches_edit(
    name: Annotated[str, typer.Argument()],
    file: Annotated[
        Path | None, typer.Option("--file", help="Save this TOML file instead of opening $EDITOR.")
    ] = None,
) -> None:
    """Edit a saved search's text and filters as TOML in $EDITOR."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            saved = workspace.get_search(conn, name)
            body = (
                file.read_text()
                if file
                else _edit_until_valid(
                    workspace.search_document(saved),
                    lambda text: documents.parse(text, SearchDocument),
                )
            )
            workspace.save_search_document(conn, name, body)
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        except DocumentError as exc:
            typer.echo(f"invalid search: {exc}", err=True)
            raise typer.Exit(1) from exc
        except _Aborted:
            typer.echo("aborted, nothing saved", err=True)
            raise typer.Exit(1) from None
    typer.echo(f"saved {name}")


@searches_app.command("run")
def searches_run(
    name: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option(help="Maximum notices to show.")] = 50,
    json_output: JsonFlag = False,
) -> None:
    """Run a saved search over active notices."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            hits = workspace.run_search(conn, name, limit=limit)
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        except query.InvalidQuery as exc:
            typer.echo(f"invalid query: {exc}", err=True)
            raise typer.Exit(1) from exc
    _print_hits(hits, json_output)


@searches_app.command("rm")
def searches_rm(name: Annotated[str, typer.Argument()]) -> None:
    """Delete a saved search."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            workspace.delete_search(conn, name)
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    typer.echo(f"deleted {name}")


def _money_short(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "-"


def _pursuit_line(p: workspace.Pursuit) -> str:
    flags = []
    if p.pwin is not None:
        flags.append(f"pwin {p.pwin}")
    if p.held_until:
        flags.append(f"held until {p.held_until}")
    if p.outcome:
        flags.append(p.outcome)
    if p.next_due:
        flags.append(f"next due {p.next_due}")
    elif p.open_tasks:
        flags.append(f"{p.open_tasks} open task(s)")
    where = p.office or p.office_code or "-"
    return f"#{p.pursuit_id}  {p.title}  @ {where}  {' · '.join(flags)}".rstrip()


@app.command("pursuits")
def pursuits_command(
    stage: Annotated[str | None, typer.Option("--stage", help="One workflow stage key.")] = None,
    all_: Annotated[bool, typer.Option("--all", help="Include closed pursuits.")] = False,
    json_output: JsonFlag = False,
) -> None:
    """Your pursuits by workflow stage, open ones unless --all."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        rows = workspace.pursuits(conn, stage=stage, include_closed=all_)
    if json_output:
        print_json([dataclasses.asdict(row) for row in rows])
        return
    if not rows:
        typer.echo("no pursuits" + (" (open)" if not all_ else ""))
    current = None
    for p in rows:
        if p.stage != current:
            current = p.stage
            typer.echo(f"{current} ({sum(1 for r in rows if r.stage == current)})")
        typer.echo("  " + _pursuit_line(p))


def _run_pursuit(
    action: Callable[[sqlite3.Connection], object], json_output: bool, done: str
) -> None:
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            result = action(conn)
        except (workspace.NotFound, ValueError, AIError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    if json_output:
        print_json(
            [dataclasses.asdict(r) for r in result]
            if isinstance(result, list)
            else dataclasses.asdict(result)
        )
    elif isinstance(result, workspace.AssessmentRecord):
        typer.echo(f"{done}:")
        for line in assess.describe(result):
            typer.echo(line)
    elif isinstance(result, list):
        typer.echo(f"{done}: {len(result)}")
        for task in result:
            typer.echo(f"  {task.task_id}  {task.stage:<10}  {task.title}")
    elif isinstance(result, workspace.Pursuit):
        typer.echo(f"{done}: " + _pursuit_line(result) + f"  [{result.stage}]")
    else:
        typer.echo(done)


PursuitId = Annotated[int, typer.Argument(metavar="PURSUIT_ID")]
Why = Annotated[str, typer.Option("--why", help="The rationale, recorded with the decision.")]


@pursuit_app.command("new")
def pursuit_new(
    title: Annotated[str, typer.Argument()],
    summary: Annotated[
        str | None, typer.Option("--summary", help="The need, in your words.")
    ] = None,
    office: Annotated[str | None, typer.Option("--office", help="Office code (the AAC).")] = None,
    naics: Annotated[str | None, typer.Option("--naics")] = None,
    notice: Annotated[str | None, typer.Option("--notice", help="A notice to link.")] = None,
    contract: Annotated[
        int | None, typer.Option("--contract", help="The incumbent contract id.")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Open a pursuit in the first stage; a notice or contract fills in office and NAICS."""
    _run_pursuit(
        lambda conn: workspace.new_pursuit(
            conn,
            title,
            summary=summary,
            office_code=office,
            naics=naics,
            notice_id=notice,
            contract_id=contract,
        ),  # fmt: skip
        json_output,
        "opened",
    )


@pursuit_app.command("show")
def pursuit_show(pursuit_id: PursuitId, json_output: JsonFlag = False) -> None:
    """One pursuit: stage and gate, tasks, notices, government dates, and its event log."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            detail = workspace.pursuit(conn, pursuit_id)
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    if json_output:
        print_json(dataclasses.asdict(detail))
        return
    p = detail.pursuit
    typer.echo(f"#{p.pursuit_id}  {p.title}")
    state = f"stage {p.stage}" + (f" → {detail.gate}" if detail.gate else "")
    if p.held_until:
        state += f" · held until {p.held_until}"
    if p.outcome:
        state += f" · {p.outcome}"
    if not p.open:
        state += f" · closed {p.closed_at}"
    typer.echo(state + (" · gate ready" if detail.gate_ready else ""))
    typer.echo(
        f"office {p.office or '-'} ({p.office_code or '-'}) · NAICS {p.naics_code or '-'}"
        f" · pwin {p.pwin if p.pwin is not None else '-'}"
    )
    if detail.incumbent:
        i = detail.incumbent
        typer.echo(
            f"incumbent {i.vendor or '-'} · {i.piid} · {_money_short(i.value_usd)}"
            f" · ends {i.pop_end or '-'}"
        )
    if p.summary:
        typer.echo(f"summary: {p.summary}")
    if p.notes:
        typer.echo(f"notes: {p.notes}")
    typer.echo("tasks:")
    for task in detail.tasks:
        mark = "x" if task.done_at else " "
        typer.echo(
            f"  [{mark}] {task.task_id:>4}  {task.due or '-':<10}  {task.stage:<10}  {task.title}"
        )
    if not detail.tasks:
        typer.echo("  (none)")
    if detail.notices:
        typer.echo("notices:")
        for n in detail.notices:
            typer.echo(f"  {n.notice_id}  {n.role:<15}  due {_day(n.response_deadline)}  {n.title}")
    if detail.related_notices:
        typer.echo("related (the incumbent's solicitation):")
        for hit in detail.related_notices:
            typer.echo(f"  {hit.notice_id}  {hit.title}")
    if detail.dates:
        typer.echo("government dates:")
        for d in detail.dates:
            typer.echo(f"  {d.date[:10]}  {d.kind:<9}  {d.label}")
    if detail.assessment:
        typer.echo("assessment:")
        for line in assess.describe(detail.assessment):
            typer.echo("  " + line)
    typer.echo("events:")
    for e in detail.events:
        change = (
            f"{e.old_value or '-'} -> {e.new_value or '-'}" if e.field != "task" else e.new_value
        )
        typer.echo(
            f"  {e.changed_at}  {e.field:<8}  {change}" + (f"  ({e.note})" if e.note else "")
        )


def _day(timestamp: str | None) -> str:
    return timestamp[:10] if timestamp else "-"


@pursuit_app.command("link")
def pursuit_link(
    pursuit_id: PursuitId,
    notice_id: Annotated[str, typer.Argument(metavar="NOTICE_ID")],
    role: Annotated[
        str | None,
        typer.Option(
            "--role",
            help="solicitation, rfi, sources-sought, presolicitation, amendment, award, other",
        ),
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Attach a notice to a pursuit; the role defaults from the notice type."""
    _run_pursuit(
        lambda conn: workspace.link_notice(conn, pursuit_id, notice_id, role=role),
        json_output,
        f"linked {notice_id} to #{pursuit_id}",
    )


@pursuit_app.command("gate")
def pursuit_gate(
    pursuit_id: PursuitId,
    decision: Annotated[str, typer.Argument(metavar="go|no-go|hold")],
    why: Why,
    until: Annotated[
        str | None, typer.Option("--until", help="Revisit date for a hold (YYYY-MM-DD).")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Record the decision at the current gate: go advances, no-go closes, hold parks."""
    _run_pursuit(
        lambda conn: workspace.gate(conn, pursuit_id, decision, why, until=until),
        json_output,
        decision,
    )


@pursuit_app.command("back")
def pursuit_back(
    pursuit_id: PursuitId,
    stage: Annotated[str, typer.Argument(metavar="STAGE")],
    why: Why,
    json_output: JsonFlag = False,
) -> None:
    """Return a pursuit to an earlier stage, with a reason."""
    _run_pursuit(
        lambda conn: workspace.move_back(conn, pursuit_id, stage, why), json_output, "moved back"
    )


@pursuit_app.command("reopen")
def pursuit_reopen(pursuit_id: PursuitId, why: Why, json_output: JsonFlag = False) -> None:
    """Reopen a closed pursuit at the stage it was in."""
    _run_pursuit(lambda conn: workspace.reopen(conn, pursuit_id, why), json_output, "reopened")


@pursuit_app.command("task")
def pursuit_task(
    pursuit_id: PursuitId,
    title: Annotated[str, typer.Argument()],
    due: Annotated[str | None, typer.Option("--due", help="YYYY-MM-DD, your date.")] = None,
    stage: Annotated[
        str | None, typer.Option("--stage", help="Default: the current stage.")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Add a task to a pursuit."""
    _run_pursuit(
        lambda conn: workspace.add_task(conn, pursuit_id, title, due=due, stage=stage),
        json_output,
        f"added task to #{pursuit_id}",
    )


@pursuit_app.command("done")
def pursuit_done(
    task_id: Annotated[int, typer.Argument(metavar="TASK_ID")], json_output: JsonFlag = False
) -> None:
    """Mark a task done."""
    _run_pursuit(
        lambda conn: workspace.complete_task(conn, task_id), json_output, f"done {task_id}"
    )


@pursuit_app.command("set")
def pursuit_set(
    pursuit_id: PursuitId,
    pwin: Annotated[int | None, typer.Option(min=0, max=100)] = None,
    notes: Annotated[str | None, typer.Option()] = None,
    title: Annotated[str | None, typer.Option()] = None,
    summary: Annotated[str | None, typer.Option()] = None,
    incumbent: Annotated[
        int | None, typer.Option("--incumbent", help="The incumbent contract id.")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Change PWin, notes, title, summary, or the incumbent."""
    _run_pursuit(
        lambda conn: workspace.update_pursuit(
            conn,
            pursuit_id,
            title=title,
            summary=summary,
            pwin=pwin,
            notes=notes,
            incumbent_contract_id=incumbent,
        ),  # fmt: skip
        json_output,
        "updated",
    )


@pursuit_app.command("outcome")
def pursuit_outcome(
    pursuit_id: PursuitId,
    outcome: Annotated[str, typer.Argument(metavar="won|lost|no-bid")],
    why: Why,
    json_output: JsonFlag = False,
) -> None:
    """Record the outcome: won moves to the last stage, lost and no-bid close the pursuit."""
    _run_pursuit(
        lambda conn: workspace.set_outcome(conn, pursuit_id, outcome, why), json_output, outcome
    )


@pursuit_app.command("assess")
def pursuit_assess(
    pursuit_id: PursuitId,
    slot: Annotated[
        str,
        typer.Option(
            "--slot", help="deep (the default; the fast slot until one is configured) or fast."
        ),
    ] = "deep",
    json_output: JsonFlag = False,
) -> None:
    """Assess the pursuit against your profile with the configured model; stored with provenance."""
    if slot not in ("fast", "deep"):
        typer.echo("--slot must be fast or deep", err=True)
        raise typer.Exit(2)
    _run_job("assess", {"pursuit_id": pursuit_id, "slot": slot}, json_output, heading="assessed:")


@pursuit_app.command("accept")
def pursuit_accept(
    pursuit_id: PursuitId,
    numbers: Annotated[list[int] | None, typer.Argument(metavar="[N ...]")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Add the latest assessment's suggested tasks (all, or the numbered ones)."""
    _run_pursuit(
        lambda conn: assess.accept_tasks(conn, pursuit_id, indices=numbers or None),
        json_output,
        "added",
    )


@pursuit_app.command("close")
def pursuit_close(pursuit_id: PursuitId, json_output: JsonFlag = False) -> None:
    """Take a finished pursuit off the board. Nothing is deleted."""
    _run_pursuit(lambda conn: workspace.close_pursuit(conn, pursuit_id), json_output, "closed")


def _print_tracked(tracked: workspace.Tracked) -> None:
    pwin = f"pwin {tracked.pwin}" if tracked.pwin is not None else "pwin -"
    typer.echo(f"{tracked.notice_id}  {tracked.response_deadline or '-'}  {pwin}  {tracked.title}")


@app.command()
def track(
    notice_id: Annotated[str, typer.Argument(metavar="NOTICE_ID")],
    stage: Annotated[workspace.Stage | None, typer.Option(help="Pipeline stage.")] = None,
    pwin: Annotated[int | None, typer.Option(min=0, max=100, help="Win probability.")] = None,
    notes: Annotated[str | None, typer.Option(help="Replace the notes.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Track a notice in your pipeline (default stage: watching) or update it."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            tracked = workspace.track(
                conn, notice_id, stage=stage.value if stage else None, pwin=pwin, notes=notes
            )
        except (workspace.NotFound, ValueError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    if json_output:
        print_json(dataclasses.asdict(tracked))
    else:
        typer.echo(f"{tracked.stage}:")
        _print_tracked(tracked)


@app.command()
def pipeline(json_output: JsonFlag = False) -> None:
    """Your tracked opportunities, grouped by stage."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        rows = workspace.pipeline(conn)
    if json_output:
        print_json([dataclasses.asdict(row) for row in rows])
        return
    if not rows:
        typer.echo("nothing tracked")
    current = None
    for tracked in rows:
        if tracked.stage != current:
            current = tracked.stage
            count = sum(1 for row in rows if row.stage == current)
            typer.echo(f"{current} ({count})")
        typer.echo("  ", nl=False)
        _print_tracked(tracked)


@app.command()
def history(
    notice_id: Annotated[str, typer.Argument(metavar="NOTICE_ID")], json_output: JsonFlag = False
) -> None:
    """The change log of a tracked opportunity."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            events = workspace.history(conn, notice_id)
        except workspace.NotFound as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    if json_output:
        print_json([dataclasses.asdict(event) for event in events])
        return
    for event in events:
        typer.echo(
            f"{event.changed_at}  {event.field}  {event.old_value or '-'} -> {event.new_value}"
        )


@profile_app.command("show")
def profile_show(json_output: JsonFlag = False) -> None:
    """Show your company profile document (or, with --json, the profile record)."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        if json_output:
            profile = workspace.get_profile(conn)
            print_json(dataclasses.asdict(profile) if profile else None)
            return
        typer.echo(workspace.profile_document(conn), nl=False)


@profile_app.command("edit")
def profile_edit(
    file: Annotated[
        Path | None, typer.Option("--file", help="Save this TOML file instead of opening $EDITOR.")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Edit your company profile as TOML in $EDITOR; every save is a new version."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            body = (
                file.read_text()
                if file
                else _edit_until_valid(
                    workspace.profile_document(conn),
                    lambda text: documents.parse(text, ProfileDocument),
                )
            )
            profile = workspace.save_profile(conn, body)
        except DocumentError as exc:
            typer.echo(f"invalid profile: {exc}", err=True)
            raise typer.Exit(1) from exc
        except _Aborted:
            typer.echo("aborted, nothing saved", err=True)
            raise typer.Exit(1) from None
        latest = workspace.latest_document(conn, "profile")
    if json_output:
        print_json(dataclasses.asdict(profile))
    else:
        typer.echo(f"saved profile version {latest.version if latest else '?'}")


@profile_app.command("history")
def profile_history(json_output: JsonFlag = False) -> None:
    """Every saved version of your profile."""
    _document_history("profile", json_output)


@workflow_app.command("show")
def workflow_show(json_output: JsonFlag = False) -> None:
    """Show your workflow document: stages, gates, and the tasks each stage starts with."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        if json_output:
            print_json(workspace.workflow(conn).model_dump())
            return
        typer.echo(workspace.workflow_document(conn), nl=False)


@workflow_app.command("edit")
def workflow_edit(
    file: Annotated[
        Path | None, typer.Option("--file", help="Save this TOML file instead of opening $EDITOR.")
    ] = None,
) -> None:
    """Edit your workflow as TOML in $EDITOR; every save is a new version."""
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        try:
            body = (
                file.read_text()
                if file
                else _edit_until_valid(
                    workspace.workflow_document(conn),
                    lambda text: documents.parse(text, WorkflowDocument),
                )
            )
            doc = workspace.save_workflow(conn, body)
        except (DocumentError, ValueError) as exc:
            typer.echo(f"invalid workflow: {exc}", err=True)
            raise typer.Exit(1) from exc
        except _Aborted:
            typer.echo("aborted, nothing saved", err=True)
            raise typer.Exit(1) from None
        latest = workspace.latest_document(conn, "workflow")
    typer.echo(
        f"saved workflow version {latest.version if latest else '?'}:"
        f" {' > '.join(stage.key for stage in doc.stages)}"
    )


@workflow_app.command("history")
def workflow_history(json_output: JsonFlag = False) -> None:
    """Every saved version of your workflow."""
    _document_history("workflow", json_output)


def _document_history(kind: str, json_output: bool) -> None:
    settings = Settings()
    with closing(db.connect(settings.db_path)) as conn:
        versions = workspace.document_versions(conn, kind)
    if json_output:
        print_json([dataclasses.asdict(v) for v in versions])
        return
    if not versions:
        typer.echo(f"no {kind} saved yet")
    for version in versions:
        typer.echo(f"v{version.version}  {version.created_at}  {len(version.body)} characters")


class _Aborted(Exception):
    """The editor returned nothing new."""


ERROR_HEADER = "# error: "


def _edit(text: str) -> str | None:
    """Open ``text`` in $VISUAL/$EDITOR; None when it was not saved or not changed."""
    return click.edit(text, extension=".toml", require_save=True)


def _edit_until_valid(text: str, validate: Callable[[str], object]) -> str:
    """Re-open the editor with the problem at the top until the document validates.
    Leaving the text unchanged aborts."""
    header = ""
    while True:
        edited = _edit(header + text)
        if edited is None or edited.strip() == (header + text).strip():
            raise _Aborted
        lines = edited.splitlines(keepends=True)
        while lines and lines[0].startswith((ERROR_HEADER, "# fix ")):
            lines.pop(0)
        text = "".join(lines)
        try:
            validate(text)
        except DocumentError as exc:
            header = (
                f"{ERROR_HEADER}{exc}\n"
                "# fix the document and save again, or leave it unchanged to abort\n"
            )
            continue
        return text


@ingest_app.command("notices")
def ingest_notices_command(
    since: DateOption = None, until: DateOption = None, json_output: JsonFlag = False
) -> None:
    """Ingest notices posted in a window (default yesterday to today, UTC) for every NAICS code."""
    _run_job("ingest-notices", {"since": since, "until": until}, json_output)


@ingest_app.command("bulk")
def ingest_bulk_command(
    archived: Annotated[
        int | None, typer.Option("--archived", help="Fiscal year of an archived extract.")
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Ingest a local extract instead of downloading.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Cap rows in the slice this run.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Backfill from the SAM.gov bulk extract: no key, no quota, filtered to your NAICS codes."""
    _run_job("ingest-bulk", {"archived": archived, "file": file, "limit": limit}, json_output)


@ingest_app.command("awards")
def ingest_awards_command(
    since: DateOption = None,
    until: DateOption = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Ingest a downloaded award file instead.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Cap rows in the slice this run.")] = None,
    json_output: JsonFlag = False,
) -> None:
    """Ingest USAspending award history for your NAICS codes: no key, no quota.

    Actions in the window (default the last three years to today) are requested as one
    download, which the service takes minutes to prepare.
    """
    _run_job(
        "ingest-awards", {"since": since, "until": until, "file": file, "limit": limit}, json_output
    )


@ingest_app.command("entities")
def ingest_entities_command(
    uei: Annotated[
        str | None,
        typer.Option("--uei", help="Comma-separated UEIs to look up (one request per ten)."),
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", help="Ingest a downloaded extract instead.")
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Download this month's extract even if one is on disk."),
    ] = False,
    limit: Annotated[
        int | None, typer.Option(help="Cap registrants in the slice this run.")
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Ingest SAM.gov entity registrations: the monthly public extract (one keyed request,
    filtered to contractors already known, your NAICS codes, and your own company), or a
    few UEIs through the Entity Management API."""
    _run_job(
        "ingest-entities",
        {"uei": uei, "file": file, "refresh": refresh, "limit": limit},
        json_output,
    )


@app.command()
def top(
    serve: Annotated[bool, typer.Option("--serve", help="Serve the UI in a browser tab.")] = False,
    port: Annotated[int, typer.Option(help="Port for --serve (localhost only).")] = 8000,
) -> None:
    """Full-screen terminal UI: dashboard, opportunities, context view, entity view."""
    settings = Settings()
    if serve:
        try:
            from textual_serve.server import Server
        except ImportError as exc:
            typer.echo("textual-serve is not installed: uv sync --extra serve", err=True)
            raise typer.Exit(2) from exc
        Server("mentor top", port=port).serve()
        return
    from mentor.tui.app import MentorTop

    MentorTop(settings, env_path=Path(".env")).run()


@app.command()
def mcp() -> None:
    """Serve the store to an MCP client over standard input and output."""
    from mentor.mcp_server import main  # heavy import; only when asked for

    main()


@db_app.command()
def migrate() -> None:
    """Apply pending schema migrations."""
    _run_job("db-migrate", {})


@db_app.command()
def reindex() -> None:
    """Rebuild the search indexes from the notice and attachment tables."""
    _run_job("db-reindex", {})


@db_app.command()
def status() -> None:
    """Show applied and pending schema migrations."""
    settings = Settings()
    typer.echo(f"database: {settings.db_path}")
    with closing(db.connect(settings.db_path)) as conn:
        current = db.status(conn)
    for name in current.applied:
        typer.echo(f"applied  {name}")
    for name in current.pending:
        typer.echo(f"pending  {name}")
