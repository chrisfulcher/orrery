"""orrery top: a full-screen terminal UI in the idiom of system monitors (DESIGN.md §3, §4).

Modes switched with number keys, each keeping its own screen stack: the dashboard (this week's
work), the opportunities table, and the pursuits board. The context view, the pursuit screen,
and the entity view are pushed on top and popped with Escape. Every read goes through the
query and workspace modules over one SQLite connection opened on mount; every query is
milliseconds, so nothing runs off the event loop.
"""

import webbrowser
from collections import Counter, deque
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Label, ListItem, ListView, Sparkline, Static
from textual.worker import Worker, WorkerState, get_current_worker

from orrery import assess, db, jobs, query, quota, summaries, workspace
from orrery.config import Settings, setup_needed
from orrery.fetch import queue
from orrery.progress import JobCancelled
from orrery.tui.setup import SetupScreen
from orrery.tui.widgets import Row, WrapTable

REFRESH_SECONDS = 5


def _day(timestamp: str | None) -> str:
    return timestamp[:10] if timestamp else "-"


def _money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "-"


AWARD_COLUMNS: list[tuple[str, int | None]] = [
    ("action", 10), ("vendor", None), ("office", 30), ("value", 14), ("set-aside", 9), ("piid", 18)
]  # fmt: skip
NOTICE_COLUMNS: list[tuple[str, int | None]] = [("deadline", 10), ("agency", 28), ("title", None)]


def _award_row(award: query.ContractRef) -> Row:
    cells = (
        _day(award.last_action_date),
        award.vendor or "-",
        award.awarding_office or "-",
        _money(award.value_usd),
        award.set_aside_code or "-",
        award.piid,
    )
    return cells, str(award.contract_id)


def _notice_row(hit: query.SearchHit) -> Row:
    return (_day(hit.response_deadline), hit.agency or "-", hit.title), hit.notice_id


WORK_COLUMNS: list[tuple[str, int | None]] = [("due", 10), ("pursuit", 30), ("task", None)]
PURSUIT_COLUMNS: list[tuple[str, int | None]] = [
    ("stage", 11), ("pwin", 4), ("next due", 10), ("office", 24), ("title", None)
]  # fmt: skip
TASK_COLUMNS: list[tuple[str, int | None]] = [
    ("done", 4),
    ("due", 10),
    ("stage", 10),
    ("task", None),
]
LINKED_COLUMNS: list[tuple[str, int | None]] = [("due", 10), ("role", 15), ("notice", None)]
EVENT_COLUMNS: list[tuple[str, int | None]] = [("when", 20), ("field", 8), ("change", None)]


def _work_row(item: workspace.WorkItem, index: int) -> Row:
    due = ("! " if item.overdue else "") + item.due
    return (due, item.pursuit_title, item.what), _pursuit_key(item.pursuit_id, index)


def _pursuit_key(pursuit_id: int, index: int) -> str:
    """A unique row key that still names the pursuit: one pursuit may fill several rows."""
    return f"{pursuit_id}:{index}"


def _pursuit_id(key: str | None) -> int | None:
    """The pursuit a row belongs to, or None for a row that is not about one (a store gap)."""
    head = key.split(":")[0] if key else ""
    return int(head) if head.isdigit() else None


def _pursuit_row(p: workspace.Pursuit) -> Row:
    stage = p.stage + (" ⏸" if p.held_until else "") + (f" {p.outcome}" if p.outcome else "")
    cells = (
        stage,
        "-" if p.pwin is None else str(p.pwin),
        p.next_due or (p.next_response_deadline or "")[:10] or "-",
        p.office or p.office_code or "-",
        p.title,
    )
    return cells, str(p.pursuit_id)


class DashboardScreen(Screen):
    def compose(self) -> ComposeResult:
        with Grid(id="panels"):
            with Vertical(id="quota", classes="panel"):
                yield Static(id="quota_text")
                yield Sparkline([], id="quota_spark", summary_function=max)
            yield Static(id="queues", classes="panel")
            with Vertical(id="activity", classes="panel"):
                yield Static(id="activity_text")
                yield Sparkline([], id="activity_spark", summary_function=max)
            yield WrapTable(WORK_COLUMNS, id="work", classes="panel", cursor_type="row")
            yield WrapTable(
                [("why", 11), ("stage", 10), ("pursuit", None)],
                id="attention",
                classes="panel",
                cursor_type="row",
            )
            yield WrapTable(
                [("date", 10), ("kind", 9), ("pursuit", 30), ("what", None)],
                id="dates",
                classes="panel",
                cursor_type="row",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#quota").border_title = "quota"
        self.query_one("#queues").border_title = "queues"
        self.query_one("#activity").border_title = "activity"
        self.refresh_panels()
        self.set_interval(REFRESH_SECONDS, self.refresh_panels)

    def on_screen_resume(self) -> None:
        self.refresh_panels()

    def refresh_jobs(self) -> None:
        self.refresh_panels()

    def refresh_panels(self) -> None:
        app = self.app
        conn, settings = app.conn, app.settings
        spent = quota.spent_today(conn)
        self.query_one("#quota_text", Static).update(
            f"spent {spent} of {settings.sam_daily_budget} today (UTC)\nlast 30 days:"
        )
        self.query_one("#quota_spark", Sparkline).data = [
            float(n) for _, n in query.quota_history(conn, days=30)
        ]
        status = queue.queue_status(conn)
        self.query_one("#queues").border_title = "queues · jobs (j)"
        self.query_one("#queues", Static).update(
            f"{status.descriptions_pending} descriptions pending\n"
            f"{status.attachments_pending} attachments pending\n"
            + "\n".join(app.job_status_lines())
        )
        counts = query.counts(conn)
        self.query_one("#activity_text", Static).update(
            f"{counts.notices} notices, {counts.active} active\n"
            f"{counts.entities} entities\nfirst seen, last 14 days:"
        )
        self.query_one("#activity_spark", Sparkline).data = [
            float(n) for _, n in query.activity(conn, days=14)
        ]
        board = workspace.dashboard(conn, days=7, stall_days=14, horizon_days=60)
        work = self.query_one("#work", WrapTable)
        overdue = sum(1 for w in board.work if w.overdue)
        work.border_title = (
            f"work this week · {len(board.work)} item(s)"
            + (f", {overdue} overdue" if overdue else "")
            if board.work
            else "work this week · nothing due (3 for pursuits)"
        )
        work.set_rows([_work_row(item, i) for i, item in enumerate(board.work)])
        attention = self.query_one("#attention", WrapTable)
        stages = ", ".join(f"{key} {n}" for key, n in board.by_stage if n)
        attention.border_title = (
            f"attention · {stages}" if stages else "attention · no open pursuits"
        )
        # Stages with work waiting come first and say the command that clears them: a store
        # that has never been summarized otherwise looks exactly like one that is caught up.
        # A caught-up store returns no gaps, so this adds nothing to a quiet panel.
        gap_rows = [
            (("ingest gap", f"{gap.pending}", f"{gap.stage} · {gap.command}"), f"gap:{i}")
            for i, gap in enumerate(query.gaps(conn, settings))
        ]
        attention.set_rows(
            gap_rows
            + [
                (
                    (item.reason, item.stage, f"{item.pursuit_title} · {item.detail}"),
                    _pursuit_key(item.pursuit_id, i),
                )
                for i, item in enumerate(board.attention)
            ]
        )
        dates = self.query_one("#dates", WrapTable)
        dates.border_title = f"government dates, next 60 days · {len(board.dates)}"
        dates.set_rows(
            [
                ((d.date[:10], d.kind, d.pursuit_title, d.label), _pursuit_key(d.pursuit_id, i))
                for i, d in enumerate(board.dates)
            ]
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        pursuit_id = _pursuit_id(event.row_key.value)
        if pursuit_id is not None:
            self.app.push_screen(PursuitScreen(pursuit_id))


def _fit(quals: summaries.Qualifications, code: str | None, stated: str | None) -> str:
    fit = quals.fit(code, stated)
    return "-" if fit == "unknown" else fit


class OpportunitiesScreen(Screen):
    BINDINGS = [Binding("escape", "focus_table", "Table", show=False)]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search notice and attachment text, Enter to run", id="query")
        yield WrapTable(
            [
                ("deadline", 10),
                ("agency", 28),
                ("type", 12),
                ("fit", 10),
                ("title", None),
                ("source", 24),
            ],
            id="hits",
            cursor_type="row",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.quals = summaries.qualifications(self.app.conn)
        self.fill(query.upcoming(self.app.conn, days=30, limit=200))
        self.query_one("#hits", WrapTable).focus()

    def fill(self, hits: list[query.SearchHit]) -> None:
        self.query_one("#hits", WrapTable).set_rows(
            [
                (
                    (
                        _day(hit.response_deadline),
                        hit.agency or "-",
                        hit.work_type or "-",
                        _fit(self.quals, hit.set_aside_code, hit.stated_set_aside),
                        escape(hit.title)
                        + (f"\n[dim]{escape(hit.summary)}[/]" if hit.summary else ""),
                        hit.source,
                    ),
                    hit.notice_id,
                )
                for hit in hits
            ]
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        conn = self.app.conn
        if not text:
            self.fill(query.upcoming(conn, days=30, limit=200))
        else:
            try:
                filters = query.Filters(active_only=True)
                self.fill(query.search(conn, text, limit=100, filters=filters))
            except query.InvalidQuery as exc:
                self.notify(f"invalid query: {exc}", severity="error")
        self.action_focus_table()

    def action_focus_table(self) -> None:
        self.query_one("#hits", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.app.push_screen(ContextScreen(event.row_key.value))


class ContextScreen(Screen):
    """The context view: one notice, its agency chain, text, documents, tracking, the
    incumbent, the office's award history, and the government contacts."""

    BINDINGS = [
        Binding("t", "pursue", "Pursue"),
        Binding("a", "agency", "Agency"),
        Binding("i", "incumbent", "Incumbent"),
        Binding("o", "open", "Open in SAM.gov"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, notice_id: str) -> None:
        super().__init__()
        self.notice_id = notice_id
        self.detail: query.NoticeDetail | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="header", classes="panel")
        yield Static(id="pursuit", classes="panel")
        yield Static(id="incumbent", classes="panel")
        yield Static(id="summary", classes="panel")
        with VerticalScroll(id="description_scroll", classes="panel"):
            yield Static(id="description")
        yield WrapTable(AWARD_COLUMNS, id="awards", classes="panel", cursor_type="row")
        yield Static(id="officials", classes="panel")
        yield WrapTable(
            [("file", None), ("fetch", 8), ("extract", 8), ("chars", 7)],
            id="attachments",
            classes="panel",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#header").border_title = "notice"
        self.query_one("#pursuit").border_title = "pursuit"
        self.query_one("#incumbent").border_title = "incumbent"
        self.query_one("#summary").border_title = "summary"
        self.query_one("#description_scroll").border_title = "description"
        self.query_one("#awards").border_title = "award history"
        self.query_one("#officials").border_title = "officials"
        self.query_one("#attachments").border_title = "documents"
        self.load()

    def load(self) -> None:
        conn = self.app.conn
        detail = query.notice(conn, self.notice_id)
        if detail is None:
            self.notify(f"no notice {self.notice_id}", severity="error")
            self.app.pop_screen()
            return
        self.detail = detail
        chain = " › ".join(ref.name for ref in detail.agency_chain) or "-"
        self.query_one("#header", Static).update(
            f"{detail.title}\n"
            f"{detail.notice_id} · {detail.solicitation_number or '-'}"
            f" · {detail.notice_type or '-'}\n"
            f"{chain}\n"
            f"set-aside {detail.set_aside_code or '-'} · NAICS {detail.naics_code or '-'}"
            f" · PSC {detail.psc_code or '-'}\n"
            f"posted {detail.posted_at or '-'} · deadline {detail.response_deadline or '-'}"
            f" · {'active' if detail.active else 'inactive'} · {detail.versions} version(s)"
            f" · source {detail.source_id}\n"
            f"{detail.url or ''}"
        )
        current = self._pursuit()
        self.query_one("#pursuit", Static).update(
            "not pursued (t to pursue)"
            if current is None
            else f"#{current.pursuit_id} {current.title} · {current.stage}"
            f"{' · ' + current.outcome if current.outcome else ''}"
            f" · pwin {current.pwin if current.pwin is not None else '-'}"
            f" · {current.open_tasks} open task(s) (t to open)"
        )
        incumbent = detail.incumbent
        self.query_one("#incumbent", Static).update(
            "none known (no award shares this solicitation or award number)"
            if incumbent is None
            else f"{incumbent.vendor or '-'} · {incumbent.piid} · {_money(incumbent.value_usd)}"
            f" · {_day(incumbent.award_date)} to {_day(incumbent.pop_end)}"
            f" · set-aside {incumbent.set_aside_code or '-'} (i to open)"
        )
        summary = self.query_one("#summary", Static)
        if detail.summary is None:
            summary.update("none yet (orrery summarize)")
            summary.border_subtitle = ""
        else:
            quals = summaries.qualifications(conn)
            fit = _fit(quals, detail.set_aside_code, detail.stated_set_aside)
            summary.update(
                f"{detail.summary}\n{detail.work_type} · set-aside"
                f" {detail.set_aside_code or detail.stated_set_aside or '-'} {fit}"
                f" · {', '.join(detail.keywords) or '-'}"
            )
            summary.border_subtitle = detail.summary_model or ""
        self.query_one("#description", Static).update(
            detail.description or f"description {detail.description_status}"
        )
        awards = self.query_one("#awards", WrapTable)
        office = detail.agency_chain[-1].name if detail.agency_chain else "this office"
        awards.border_title = (
            f"award history · {office} · NAICS {detail.naics_code or '-'}"
            if detail.award_history
            else "award history · none in the store (orrery ingest awards)"
        )
        awards.set_rows([_award_row(award) for award in detail.award_history])
        self.query_one("#officials", Static).update(
            "\n".join(
                f"{official.name} ({official.kind}"
                f"{', ' + official.title if official.title else ''})"
                f" · {official.email or '-'} · {official.phone or '-'}"
                f"{' · fax ' + official.fax if official.fax else ''}"
                f"{' · ' + official.office_address if official.office_address else ''}"
                f" · {official.other_notices} other notice(s)"
                for official in detail.officials
            )
            or "none named"
        )
        self.query_one("#attachments", WrapTable).set_rows(
            [
                (
                    (
                        item.filename or item.url.rsplit("/", 2)[-2],
                        item.fetch_status,
                        item.extract_status,
                        str(item.text_chars),
                    ),
                    str(item.attachment_id),
                )
                for item in detail.attachments
            ]
        )

    def _pursuit(self) -> workspace.Pursuit | None:
        return workspace.pursuit_for_notice(self.app.conn, self.notice_id)

    def action_pursue(self) -> None:
        """Open the pursuit this notice belongs to, or attach the notice to one."""
        current = self._pursuit()
        if current is not None and current.open:
            self.app.push_screen(PursuitScreen(current.pursuit_id))
            return
        office = (
            self.detail.agency_chain[-1].entity_id
            if self.detail and self.detail.agency_chain
            else None
        )
        candidates = sorted(
            workspace.pursuits(self.app.conn),
            key=lambda p: (p.office_entity_id != office, p.title.lower()),
        )
        self.app.push_screen(PursueModal(candidates), self._pursued)

    def _pursued(self, choice: int | str | None) -> None:
        if choice is None or self.detail is None:
            return
        if choice == "new":
            opened = workspace.new_pursuit(
                self.app.conn, self.detail.title, notice_id=self.notice_id
            )
            pursuit_id = opened.pursuit_id
        else:
            workspace.link_notice(self.app.conn, int(choice), self.notice_id)
            pursuit_id = int(choice)
        self.load()
        self.app.push_screen(PursuitScreen(pursuit_id))

    def action_agency(self) -> None:
        if self.detail and self.detail.agency_chain:
            self.app.push_screen(EntityScreen(self.detail.agency_chain[-1].entity_id))

    def action_incumbent(self) -> None:
        if self.detail and self.detail.incumbent and self.detail.incumbent.vendor_entity_id:
            self.app.push_screen(EntityScreen(self.detail.incumbent.vendor_entity_id))

    def action_open(self) -> None:
        if self.detail and self.detail.url:
            webbrowser.open(self.detail.url)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "awards" and self.detail:
            award = next(
                (a for a in self.detail.award_history if str(a.contract_id) == event.row_key.value),
                None,
            )
            if award and award.vendor_entity_id:
                self.app.push_screen(EntityScreen(award.vendor_entity_id))


class PromptModal(ModalScreen[str | None]):
    """One line of text; Enter returns it, Escape returns None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, label: str, initial: str = "", *, placeholder: str = "") -> None:
        super().__init__()
        self.label = label
        self.initial = initial
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal_box"):
            yield Label(self.label)
            yield Input(value=self.initial, placeholder=self.placeholder, id="prompt")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DecisionModal(ModalScreen[dict | None]):
    """A choice plus its rationale (and a date when the choice is a hold): Enter on the
    choice moves to the rationale, Enter there records the decision."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, choices: list[str], *, date_for: str | None = None) -> None:
        super().__init__()
        self.title_text = title
        self.choices = choices
        self.date_for = date_for

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal_box"):
            yield Label(self.title_text)
            yield ListView(*[ListItem(Label(c), name=c) for c in self.choices], id="choices")
            yield Input(placeholder="why (recorded with the decision)", id="why")
            if self.date_for:
                yield Input(
                    placeholder=f"revisit date for a {self.date_for}, YYYY-MM-DD", id="until"
                )

    def on_mount(self) -> None:
        self.query_one("#choices", ListView).focus()

    def _choice(self) -> str:
        choices = self.query_one("#choices", ListView)
        index = choices.index if choices.index is not None else 0
        return self.choices[index]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.query_one("#why", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        choice = self._choice()
        why = self.query_one("#why", Input).value.strip()
        until = self.query_one("#until", Input).value.strip() if self.date_for else ""
        if not why:
            self.query_one("#why", Input).focus()
            return
        if event.input.id == "why" and self.date_for and choice == self.date_for and not until:
            self.query_one("#until", Input).focus()
            return
        self.dismiss({"choice": choice, "why": why, "until": until or None})

    def action_cancel(self) -> None:
        self.dismiss(None)


class TaskModal(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal_box"):
            yield Label("new task (Enter on the date to add, Escape to cancel)")
            yield Input(placeholder="what", id="title")
            yield Input(placeholder="due YYYY-MM-DD (optional)", id="due")

    def on_mount(self) -> None:
        self.query_one("#title", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        title = self.query_one("#title", Input).value.strip()
        if not title:
            self.query_one("#title", Input).focus()
            return
        if event.input.id == "title":
            self.query_one("#due", Input).focus()
            return
        self.dismiss({"title": title, "due": self.query_one("#due", Input).value.strip() or None})

    def action_cancel(self) -> None:
        self.dismiss(None)


class PursueModal(ModalScreen[int | str | None]):
    """Attach a notice to an open pursuit, or start a new one from it."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, candidates: list[workspace.Pursuit]) -> None:
        super().__init__()
        self.candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal_box"):
            yield Label("pursue (Enter to choose, Escape to cancel)")
            items = [ListItem(Label("new pursuit from this notice"), name="new")]
            items += [
                ListItem(
                    Label(f"#{p.pursuit_id} {p.title} · {p.stage} @ {p.office or '-'}"),
                    name=str(p.pursuit_id),
                )
                for p in self.candidates
            ]
            yield ListView(*items, id="pursue_choices")

    def on_mount(self) -> None:
        self.query_one("#pursue_choices", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = event.item.name
        self.dismiss("new" if name == "new" else int(name))

    def action_cancel(self) -> None:
        self.dismiss(None)


class PursuitsScreen(Screen):
    """The board: open pursuits by stage."""

    BINDINGS = [
        Binding("n", "new", "New pursuit"),
        Binding("c", "toggle_closed", "Closed"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.include_closed = False

    def compose(self) -> ComposeResult:
        yield WrapTable(PURSUIT_COLUMNS, id="board", classes="panel", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_board()
        self.query_one("#board", WrapTable).focus()

    def on_screen_resume(self) -> None:
        self.refresh_board()

    def refresh_board(self) -> None:
        conn = self.app.conn
        rows = workspace.pursuits(conn, include_closed=self.include_closed)
        board = self.query_one("#board", WrapTable)
        counts = Counter(p.stage for p in rows if p.open)
        stages = ", ".join(f"{k} {counts[k]}" for k in workspace.workflow(conn).keys() if counts[k])
        board.border_title = f"pursuits · {stages or 'none open'}" + (
            " · closed shown" if self.include_closed else ""
        )
        board.set_rows([_pursuit_row(p) for p in rows])

    def action_toggle_closed(self) -> None:
        self.include_closed = not self.include_closed
        self.refresh_board()

    def action_new(self) -> None:
        self.app.push_screen(
            PromptModal("new pursuit: title", placeholder="the requirement"), self._new
        )

    def _new(self, title: str | None) -> None:
        if title and title.strip():
            opened = workspace.new_pursuit(self.app.conn, title.strip())
            self.refresh_board()
            self.app.push_screen(PursuitScreen(opened.pursuit_id))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.app.push_screen(PursuitScreen(int(event.row_key.value)))


RADAR_COLUMNS: list[tuple[str, int | None]] = [
    ("ends", 10), ("current", 10), ("vendor", None), ("office", 26), ("value", 13),
    ("set-aside", 9), ("piid", 18),
]  # fmt: skip


class RadarScreen(Screen):
    """The recompete radar: awards ending soonest, options included, in the profile's NAICS."""

    BINDINGS = [
        Binding("p", "pursue", "Pursue"),
        Binding("m", "more", "Wider window"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.months = 18
        self.rows: list[query.ContractRef] = []

    def compose(self) -> ComposeResult:
        yield WrapTable(RADAR_COLUMNS, id="radar", classes="panel", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_radar()
        self.query_one("#radar", WrapTable).focus()

    def on_screen_resume(self) -> None:
        self.refresh_radar()

    def refresh_radar(self) -> None:
        conn = self.app.conn
        profile = workspace.get_profile(conn)
        naics = profile.naics if profile and profile.naics else None
        self.rows = query.recompetes(conn, months=self.months, naics=naics, limit=500)
        table = self.query_one("#radar", WrapTable)
        scope = (
            f"NAICS {', '.join(naics)}" if naics else "every NAICS (set the profile's offerings)"
        )
        table.border_title = f"recompetes, next {self.months} months · {scope} · {len(self.rows)}"
        table.set_rows(
            [
                (
                    (
                        award.pop_potential_end or award.pop_end or "-",
                        award.pop_end or "-",
                        award.vendor or "-",
                        award.awarding_office or "-",
                        _money(award.value_usd),
                        award.set_aside_code or "-",
                        award.piid,
                    ),
                    str(award.contract_id),
                )
                for award in self.rows
            ]
        )

    def action_more(self) -> None:
        self.months = {18: 36, 36: 6}.get(self.months, 18)
        self.refresh_radar()

    def _selected(self) -> query.ContractRef | None:
        table = self.query_one("#radar", WrapTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((a for a in self.rows if str(a.contract_id) == key), None)

    def action_pursue(self) -> None:
        award = self._selected()
        if award is None:
            return
        opened = workspace.new_pursuit(
            self.app.conn,
            f"Recompete: {award.piid} · {award.vendor or 'unknown vendor'}",
            contract_id=award.contract_id,
        )
        self.app.push_screen(PursuitScreen(opened.pursuit_id))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        award = self._selected()
        if award and award.vendor_entity_id:
            self.app.push_screen(EntityScreen(award.vendor_entity_id))


class PursuitScreen(Screen):
    """One pursuit: where it stands, its tasks, its notices, and every decision."""

    BINDINGS = [
        Binding("d", "done", "Done"),
        Binding("t", "task", "Task"),
        Binding("g", "gate", "Gate"),
        Binding("b", "back", "Back a stage"),
        Binding("w", "outcome", "Outcome"),
        Binding("p", "pwin", "PWin"),
        Binding("n", "notes", "Notes"),
        Binding("a", "office", "Office"),
        Binding("i", "incumbent", "Incumbent"),
        Binding("x", "accept_tasks", "Accept suggested tasks"),
        Binding("s", "assess", "Assess"),
        Binding("escape", "app.pop_screen", "Back"),
    ]

    def __init__(self, pursuit_id: int) -> None:
        super().__init__()
        self.pursuit_id = pursuit_id
        self.detail: workspace.PursuitDetail | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="pursuit_header", classes="panel")
        yield Static(id="assessment", classes="panel")
        yield WrapTable(TASK_COLUMNS, id="tasks", classes="panel", cursor_type="row")
        yield WrapTable(LINKED_COLUMNS, id="pursuit_notices", classes="panel", cursor_type="row")
        yield WrapTable(EVENT_COLUMNS, id="events", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#pursuit_header").border_title = "pursuit"
        self.query_one("#assessment").border_title = (
            f"assessment · {self._assessing()}" if self._assessing() else "assessment"
        )
        self.query_one("#tasks").border_title = "tasks"
        self.query_one("#pursuit_notices").border_title = "notices"
        self.query_one("#events").border_title = "decisions and changes"
        self.load()
        self.query_one("#tasks", WrapTable).focus()

    def load(self) -> None:
        try:
            detail = workspace.pursuit(self.app.conn, self.pursuit_id)
        except workspace.NotFound:
            self.notify(f"no pursuit {self.pursuit_id}", severity="error")
            self.app.pop_screen()
            return
        self.detail = detail
        p = detail.pursuit
        state = f"{p.stage}" + (f" → {detail.gate}" if detail.gate else "")
        if p.held_until:
            state += f" · held until {p.held_until}"
        if p.outcome:
            state += f" · {p.outcome}"
        if not p.open:
            state += " · closed"
        if detail.gate_ready:
            state += " · gate ready (g)"
        incumbent = ""
        if detail.incumbent:
            i = detail.incumbent
            incumbent = (
                f"\nincumbent {i.vendor or '-'} · {i.piid} · {_money(i.value_usd)}"
                f" · ends {i.pop_end or '-'} (i to open)"
            )
        dates = " · ".join(f"{d.date[:10]} {d.kind} {d.label}" for d in detail.dates)
        self.query_one("#pursuit_header", Static).update(
            f"#{p.pursuit_id} {p.title}\n{state}\n"
            f"office {p.office or '-'} ({p.office_code or '-'}) · NAICS {p.naics_code or '-'}"
            f" · pwin {p.pwin if p.pwin is not None else '-'}{incumbent}"
            + (f"\nsummary: {p.summary}" if p.summary else "")
            + (f"\nnotes: {p.notes}" if p.notes else "")
            + (f"\ngovernment dates: {dates}" if dates else "")
        )
        self.query_one("#tasks", WrapTable).set_rows(
            [
                (
                    ("x" if task.done_at else " ", task.due or "-", task.stage, task.title),
                    str(task.task_id),
                )
                for task in detail.tasks
            ]
        )
        self.query_one("#pursuit_notices", WrapTable).set_rows(
            [((_day(n.response_deadline), n.role, n.title), n.notice_id) for n in detail.notices]
        )
        self.query_one("#assessment", Static).update(
            "\n".join(assess.describe(detail.assessment))
            if detail.assessment
            else self._assessing() or "none yet: s to assess (x accepts suggested tasks)"
        )
        self.query_one("#events", WrapTable).set_rows(
            [
                (
                    (
                        e.changed_at,
                        e.field,
                        (
                            e.new_value or "-"
                            if e.field == "task"
                            else f"{e.old_value or '-'} → {e.new_value or '-'}"
                        )
                        + (f" ({e.note})" if e.note else ""),
                    ),
                    str(e.event_id),
                )
                for e in reversed(detail.events)
            ]
        )

    def _selected_task(self) -> workspace.Task | None:
        table = self.query_one("#tasks", WrapTable)
        if not self.detail or not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((t for t in self.detail.tasks if str(t.task_id) == key), None)

    def _apply(self, action: Callable[[], object]) -> None:
        try:
            action()
        except (ValueError, workspace.NotFound) as exc:
            self.notify(str(exc), severity="error")
        self.load()

    def action_done(self) -> None:
        task = self._selected_task()
        if task is not None and task.done_at is None:
            self._apply(lambda: workspace.complete_task(self.app.conn, task.task_id))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "tasks":
            self.action_done()
        elif event.data_table.id == "pursuit_notices" and event.row_key.value:
            self.app.push_screen(ContextScreen(event.row_key.value))

    def action_task(self) -> None:
        self.app.push_screen(TaskModal(), self._task_added)

    def _task_added(self, result: dict | None) -> None:
        if result:
            self._apply(
                lambda: workspace.add_task(
                    self.app.conn, self.pursuit_id, result["title"], due=result["due"]
                )
            )

    def action_gate(self) -> None:
        if self.detail and self.detail.gate:
            self.app.push_screen(
                DecisionModal(
                    f"{self.detail.gate}: decision", ["go", "no-go", "hold"], date_for="hold"
                ),
                self._gated,
            )
        else:
            self.notify("this stage has no gate", severity="warning")

    def _gated(self, result: dict | None) -> None:
        if result:
            self._apply(
                lambda: workspace.gate(
                    self.app.conn,
                    self.pursuit_id,
                    result["choice"],
                    result["why"],
                    until=result["until"],
                )
            )

    def action_back(self) -> None:
        if not self.detail:
            return
        earlier = (
            workspace.workflow(self.app.conn).previous_keys(self.detail.pursuit.stage)
            if self.detail.pursuit.stage in workspace.workflow(self.app.conn).keys()
            else []
        )
        if not earlier:
            self.notify("already at the first stage", severity="warning")
            return
        self.app.push_screen(
            DecisionModal("move back to", list(reversed(earlier))), self._moved_back
        )

    def _moved_back(self, result: dict | None) -> None:
        if result:
            self._apply(
                lambda: workspace.move_back(
                    self.app.conn, self.pursuit_id, result["choice"], result["why"]
                )
            )

    def action_outcome(self) -> None:
        if self.detail and not self.detail.pursuit.open:
            self.app.push_screen(DecisionModal("reopen", ["reopen"]), self._reopened)
        else:
            self.app.push_screen(DecisionModal("outcome", ["won", "lost", "no-bid"]), self._outcome)

    def _reopened(self, result: dict | None) -> None:
        if result:
            self._apply(lambda: workspace.reopen(self.app.conn, self.pursuit_id, result["why"]))

    def _outcome(self, result: dict | None) -> None:
        if result:
            self._apply(
                lambda: workspace.set_outcome(
                    self.app.conn, self.pursuit_id, result["choice"], result["why"]
                )
            )

    def action_pwin(self) -> None:
        current = self.detail.pursuit.pwin if self.detail else None
        self.app.push_screen(
            PromptModal("win probability, 0 to 100", "" if current is None else str(current)),
            self._pwin,
        )

    def _pwin(self, value: str | None) -> None:
        if value is None or not value.strip():
            return
        try:
            pwin = int(value)
        except ValueError:
            self.notify("pwin must be a whole number", severity="error")
            return
        self._apply(lambda: workspace.update_pursuit(self.app.conn, self.pursuit_id, pwin=pwin))

    def action_notes(self) -> None:
        current = self.detail.pursuit.notes if self.detail else None
        self.app.push_screen(PromptModal("notes", current or ""), self._notes)

    def _notes(self, value: str | None) -> None:
        if value is not None:
            self._apply(
                lambda: workspace.update_pursuit(self.app.conn, self.pursuit_id, notes=value)
            )

    def action_office(self) -> None:
        if self.detail and self.detail.pursuit.office_entity_id:
            self.app.push_screen(EntityScreen(self.detail.pursuit.office_entity_id))

    def action_incumbent(self) -> None:
        if self.detail and self.detail.incumbent and self.detail.incumbent.vendor_entity_id:
            self.app.push_screen(EntityScreen(self.detail.incumbent.vendor_entity_id))

    def _assessing(self) -> str | None:
        run = self.app.current
        if (
            run
            and run.finished_at is None
            and run.job.name == "assess"
            and int(run.params.get("pursuit_id", 0)) == self.pursuit_id
        ):
            return f"assessing with the {run.params.get('slot', 'deep')} model… (j for the log)"
        return None

    def refresh_jobs(self) -> None:
        self.load()

    def action_assess(self) -> None:
        self.app.run_job(
            "assess", {"pursuit_id": self.pursuit_id, "slot": "deep"}, on_done=self.load
        )

    def action_accept_tasks(self) -> None:
        if not self.detail or not self.detail.assessment:
            self.notify("no assessment yet", severity="warning")
            return
        added: list[workspace.Task] = []
        self._apply(lambda: added.extend(assess.accept_tasks(self.app.conn, self.pursuit_id)))
        self.notify(f"added {len(added)} task(s)")


class EntityScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, entity_id: int) -> None:
        super().__init__()
        self.entity_id = entity_id
        self.detail: query.EntityDetail | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="entity_header", classes="panel")
        yield WrapTable(AWARD_COLUMNS, id="entity_awards", classes="panel", cursor_type="row")
        yield WrapTable(NOTICE_COLUMNS, id="entity_notices", classes="panel", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        detail = query.entity(self.app.conn, self.entity_id, recent=50)
        self.detail = detail
        header = self.query_one("#entity_header", Static)
        header.border_title = "entity"
        awards = self.query_one("#entity_awards", WrapTable)
        table = self.query_one("#entity_notices", WrapTable)
        table.border_title = "recent notices"
        if detail is None:
            header.update(f"no entity {self.entity_id}")
            return
        contractor = detail.kind == "contractor"
        if contractor:
            keys = f"uei {detail.uei or '-'} · cage {detail.cage or '-'}"
            others = ", ".join(a for a in detail.aliases if a != detail.name) or "-"
            second = f"also seen as: {others}"
        else:
            keys = f"path {detail.path_code or '-'} · {detail.notices} notice(s)"
            second = f"offices: {', '.join(child.name for child in detail.children) or '-'}"
        facts = "\n".join(
            f"{predicate}: {value}"
            for predicate, value in query.summarize_facts(detail.facts, max_items=8)
        )
        header.update(
            f"{detail.kind}: {detail.name}\n"
            f"{' › '.join(ref.name for ref in detail.chain)}\n"
            f"{keys} · {detail.awards_count} award(s), {_money(detail.awards_value_usd)}\n"
            f"{second}" + (f"\n{facts}" if facts else "")
        )
        awards.border_title = "awards won" if contractor else "awards made"
        awards.set_rows([_award_row(award) for award in detail.awards])
        table.set_rows([_notice_row(hit) for hit in detail.recent])
        (awards if contractor and detail.awards else table).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "entity_notices" and event.row_key.value:
            self.app.push_screen(ContextScreen(event.row_key.value))
        elif event.data_table.id == "entity_awards" and self.detail:
            award = next(
                (a for a in self.detail.awards if str(a.contract_id) == event.row_key.value), None
            )
            if award is None:
                return
            other = (
                award.awarding_entity_id
                if self.detail.kind == "contractor"
                else award.vendor_entity_id
            )
            if other and other != self.entity_id:
                self.app.push_screen(EntityScreen(other))


@dataclass
class JobRun:
    """One job as the app runs it: its lines, its worker, and how it ended."""

    job: jobs.Job
    params: dict
    started_at: str
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=2000))
    worker: Worker | None = None
    result: object = None
    error: str | None = None
    finished_at: str | None = None
    on_done: Callable[[], None] | None = None

    @property
    def state(self) -> str:
        if self.finished_at is None:
            return "cancelling" if self.worker and self.worker.is_cancelled else "running"
        return self.error or "done"

    @property
    def last_line(self) -> str:
        return self.lines[-1] if self.lines else ""


class OrreryTop(App):
    TITLE = "orrery"
    CSS_PATH = "top.tcss"
    MODES = {
        "dashboard": DashboardScreen,
        "opportunities": OpportunitiesScreen,
        "pursuits": PursuitsScreen,
        "radar": RadarScreen,
        "setup": SetupScreen,
    }
    BINDINGS = [
        Binding("1", "switch_mode('dashboard')", "Dashboard"),
        Binding("2", "switch_mode('opportunities')", "Opportunities"),
        Binding("3", "switch_mode('pursuits')", "Pursuits"),
        Binding("4", "switch_mode('radar')", "Radar"),
        Binding("5", "switch_mode('setup')", "Setup"),
        Binding("j", "jobs", "Jobs", show=False),
        Binding("slash", "search", "Search"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, settings: Settings, *, env_path: Path | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.env_path = env_path
        """Where the Connections tab writes settings; None means session-only."""
        self.conn = None
        self.current: JobRun | None = None
        self.history: list[JobRun] = []

    def on_mount(self) -> None:
        self.theme = "ansi-dark"  # the terminal's own palette
        self.conn = db.connect(self.settings.db_path)
        applied = db.migrate(self.conn)
        if applied:
            self.notify(f"applied {len(applied)} migration(s)")
        self.switch_mode("setup" if setup_needed(self.settings, self.env_path) else "dashboard")

    async def action_jobs(self) -> None:
        await self.switch_mode("setup")
        if isinstance(self.screen, SetupScreen):
            self.screen.show_tab("jobs")

    def run_job(
        self, name: str, params: dict, *, on_done: Callable[[], None] | None = None
    ) -> bool:
        """Start a job in a thread on its own connection; one at a time."""
        if self.current is not None and self.current.finished_at is None:
            self.notify(f"{self.current.job.label} is still running", severity="warning")
            return False
        job = jobs.JOBS[name]
        unmet = jobs.needs_unmet(job, self.settings, params)
        if unmet:
            self.notify("; ".join(unmet), severity="error")
            self.run_worker(self._open_connections(), exclusive=False)
            return False
        try:
            jobs.coerce(job, params)
        except jobs.JobFailed as exc:
            self.notify(str(exc), severity="error")
            return False
        run = JobRun(job, params, db.utcnow(), on_done=on_done)
        self.current = run
        run.worker = self._job_worker(run)
        self.refresh_job_views()
        return True

    async def _open_connections(self) -> None:
        await self.switch_mode("setup")
        if isinstance(self.screen, SetupScreen):
            self.screen.show_tab("connections")

    @work(thread=True, exit_on_error=False, group="jobs")
    def _job_worker(self, run: JobRun) -> object:
        worker = get_current_worker()
        settings = self.settings
        with closing(db.connect(settings.db_path)) as conn:
            return jobs.run(
                run.job, conn, settings, run.params,
                report=lambda message: self.call_from_thread(self._job_line, run, message),
                cancelled=lambda: worker.is_cancelled,
            )  # fmt: skip

    def _job_line(self, run: JobRun, message: str) -> None:
        run.lines.append(message)
        self.refresh_job_views()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        run = self.current
        if event.worker.group != "jobs" or run is None or event.worker is not run.worker:
            return
        if event.state == WorkerState.SUCCESS:
            run.result = event.worker.result
            run.lines.append(jobs.summarize(run.job, run.result))
            self.notify(f"{run.job.label}: done")
        elif event.state == WorkerState.ERROR:
            error = event.worker.error
            run.error = "cancelled" if isinstance(error, JobCancelled) else str(error)
            run.lines.append(run.error)
            self.notify(f"{run.job.label}: {run.error}", severity="error")
        elif event.state == WorkerState.CANCELLED:
            run.error = "cancelled"
            run.lines.append("cancelled")
        else:
            return
        run.finished_at = db.utcnow()
        self.history.append(run)
        del self.history[:-10]
        self.refresh_job_views()
        if run.on_done is not None:
            run.on_done()

    def action_cancel_job(self) -> None:
        run = self.current
        if run is None or run.finished_at is not None or run.worker is None:
            return
        if not run.job.cancellable:
            self.notify(f"{run.job.label} cannot be cancelled", severity="warning")
            return
        run.worker.cancel()
        run.lines.append("cancelling after the current item")
        self.refresh_job_views()

    def refresh_job_views(self) -> None:
        """Tell whatever is on screen that job state changed."""
        screen = self.screen
        refresh = getattr(screen, "refresh_jobs", None)
        if refresh is not None:
            refresh()

    def job_status_lines(self) -> list[str]:
        """Two lines for the dashboard: what is running, and the last result."""
        current = self.current
        running = (
            f"running: {current.job.label} · {current.last_line or 'starting'}"
            if current and current.finished_at is None
            else "idle"
        )
        last = next((r for r in reversed(self.history)), None)
        return [
            running,
            f"last: {last.job.label} · {last.last_line[:60]} ({last.finished_at[11:16]})"
            if last
            else "last: none this session",
        ]

    async def show_hits(self, hits: list[query.SearchHit], title: str) -> None:
        """Switch to the opportunities table with these hits (a saved search's results)."""
        await self.switch_mode("opportunities")
        screen = self.screen
        if isinstance(screen, OpportunitiesScreen):
            screen.fill(hits)
            screen.query_one("#hits", WrapTable).border_title = title

    def reload_settings(self) -> None:
        """Re-read settings after the Connections tab wrote .env."""
        if self.env_path is not None:
            self.settings = Settings(_env_file=self.env_path)

    async def action_search(self) -> None:
        await self.switch_mode("opportunities")
        self.screen.query_one("#query", Input).focus()
