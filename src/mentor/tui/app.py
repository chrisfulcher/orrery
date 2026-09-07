"""mentor top: a full-screen terminal UI in the idiom of system monitors (DESIGN.md §3, §4).

Modes switched with number keys, each keeping its own screen stack: the dashboard (this week's
work), the opportunities table, and the pursuits board. The context view, the pursuit screen,
and the entity view are pushed on top and popped with Escape. Every read goes through the
query and workspace modules over one SQLite connection opened on mount; every query is
milliseconds, so nothing runs off the event loop.
"""

import webbrowser
from collections import Counter
from collections.abc import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Label, ListItem, ListView, Sparkline, Static

from mentor import assess, db, query, quota, workspace
from mentor.config import Settings
from mentor.fetch import queue

REFRESH_SECONDS = 5


def _day(timestamp: str | None) -> str:
    return timestamp[:10] if timestamp else "-"


def _money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "-"


Row = tuple[tuple[str, ...], str | None]
"""Cells and the row key."""


class WrapTable(DataTable):
    """A table whose text wraps instead of truncating: every column has a declared width
    except one, which takes the width that is left, and rows grow to fit their text. The
    table keeps its rows and lays them out again whenever it is resized."""

    MIN_FLEX = 12
    COMFORTABLE_FLEX = 32
    MIN_SHRINK = 12
    """Fixed columns wider than this give width back, down to this, before the flexible
    column drops below COMFORTABLE_FLEX."""

    def __init__(self, columns: list[tuple[str, int | None]], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.columns_spec = columns
        self.rows_data: list[Row] = []

    def set_rows(self, rows: list[Row]) -> None:
        """Show these rows. Unchanged rows leave the table, its cursor, and its scroll alone,
        so a periodic refresh never disturbs what the user is looking at."""
        if rows == self.rows_data and self.columns:
            return
        self.rows_data = rows
        self._layout_rows()

    def on_resize(self) -> None:
        self._layout_rows()

    def _layout_rows(self) -> None:
        selected = self._selected_key()
        self._rebuild()
        if selected is not None:
            self._select(selected)

    def _selected_key(self) -> str | None:
        if not self.row_count:
            return None
        try:
            return self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value
        except Exception:  # no cell under the cursor
            return None

    def _select(self, key: str) -> None:
        try:
            index = self.get_row_index(key)
        except Exception:  # the row is gone; the cursor stays at the top
            return
        self.move_cursor(row=index, animate=False, scroll=True)

    def _rebuild(self) -> None:
        pad = 2 * self.cell_padding
        widths = [width for _, width in self.columns_spec]
        available = self.content_size.width - pad * len(widths)

        def flex() -> int:
            return available - sum(width for width in widths if width is not None)

        while flex() < self.COMFORTABLE_FLEX:
            widest = max(
                (i for i, w in enumerate(widths) if w is not None and w > self.MIN_SHRINK),
                key=lambda i: widths[i],
                default=None,
            )
            if widest is None:
                break
            widths[widest] -= 1
        self.clear(columns=True)
        for (label, _), width in zip(self.columns_spec, widths, strict=True):
            self.add_column(label, width=max(flex(), self.MIN_FLEX) if width is None else width)
        for cells, key in self.rows_data:
            self.add_row(*cells, height=None, key=key)


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
    return int(key.split(":")[0]) if key else None


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
        self.query_one("#queues", Static).update(
            f"{status.descriptions_pending} descriptions pending\n"
            f"{status.attachments_pending} attachments pending"
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
        attention.set_rows(
            [
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


class OpportunitiesScreen(Screen):
    BINDINGS = [Binding("escape", "focus_table", "Table", show=False)]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search notice and attachment text, Enter to run", id="query")
        yield WrapTable(
            [("deadline", 10), ("agency", 28), ("title", None), ("source", 24)],
            id="hits",
            cursor_type="row",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.fill(query.upcoming(self.app.conn, days=30, limit=200))
        self.query_one("#hits", WrapTable).focus()

    def fill(self, hits: list[query.SearchHit]) -> None:
        self.query_one("#hits", WrapTable).set_rows(
            [
                (
                    (_day(hit.response_deadline), hit.agency or "-", hit.title, hit.source),
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
        self.query_one("#description", Static).update(
            detail.description or f"description {detail.description_status}"
        )
        awards = self.query_one("#awards", WrapTable)
        office = detail.agency_chain[-1].name if detail.agency_chain else "this office"
        awards.border_title = (
            f"award history · {office} · NAICS {detail.naics_code or '-'}"
            if detail.award_history
            else "award history · none in the store (mentor ingest awards)"
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
        self.query_one("#assessment").border_title = "assessment"
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
            else f"none yet: run `mentor pursuit assess {p.pursuit_id}` (x accepts suggested tasks)"
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


class MentorTop(App):
    TITLE = "mentor"
    CSS_PATH = "top.tcss"
    MODES = {
        "dashboard": DashboardScreen,
        "opportunities": OpportunitiesScreen,
        "pursuits": PursuitsScreen,
        "radar": RadarScreen,
    }
    BINDINGS = [
        Binding("1", "switch_mode('dashboard')", "Dashboard"),
        Binding("2", "switch_mode('opportunities')", "Opportunities"),
        Binding("3", "switch_mode('pursuits')", "Pursuits"),
        Binding("4", "switch_mode('radar')", "Radar"),
        Binding("slash", "search", "Search"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.conn = None

    def on_mount(self) -> None:
        self.theme = "ansi-dark"  # the terminal's own palette
        self.conn = db.connect(self.settings.db_path)
        self.switch_mode("dashboard")

    async def action_search(self) -> None:
        await self.switch_mode("opportunities")
        self.screen.query_one("#query", Input).focus()
