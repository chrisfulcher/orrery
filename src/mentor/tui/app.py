"""mentor top: a full-screen terminal UI in the idiom of system monitors (DESIGN.md §3, §4).

Two modes switched with number keys, each keeping its own screen stack: the dashboard and the
opportunities table. The context view and the entity view are pushed on top and popped with
Escape. Every read goes through the query and workspace modules over one SQLite connection
opened on mount; every query is milliseconds, so nothing runs off the event loop.
"""

import webbrowser
from collections import Counter

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Label, ListItem, ListView, Sparkline, Static

from mentor import db, query, quota, workspace
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
            yield WrapTable(NOTICE_COLUMNS, id="deadlines", classes="panel", cursor_type="row")
            yield WrapTable(
                [("deadline", 10), ("stage", 9), ("pwin", 4), ("title", None)],
                id="pipeline",
                classes="panel",
                cursor_type="row",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#quota").border_title = "quota"
        self.query_one("#queues").border_title = "queues"
        self.query_one("#activity").border_title = "activity"
        self.query_one("#deadlines").border_title = "deadlines, next 7 days"
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
        self.query_one("#deadlines", WrapTable).set_rows(
            [_notice_row(hit) for hit in query.upcoming(conn, days=7, limit=20)]
        )
        pipeline = self.query_one("#pipeline", WrapTable)
        rows = workspace.pipeline(conn)
        by_stage = Counter(row.stage for row in rows)
        summary = ", ".join(
            f"{stage} {by_stage[stage]}"
            for stage in workspace.workflow(conn).keys()
            if by_stage[stage]
        )
        pipeline.border_title = f"pipeline · {summary}" if summary else "pipeline · nothing tracked"
        pipeline.set_rows(
            [
                (
                    (
                        _day(tracked.response_deadline),
                        tracked.stage,
                        "-" if tracked.pwin is None else str(tracked.pwin),
                        tracked.title,
                    ),
                    tracked.notice_id,
                )
                for tracked in rows[:10]
            ]
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.app.push_screen(ContextScreen(event.row_key.value))


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
        Binding("t", "track", "Track / stage"),
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
        yield Static(id="tracking", classes="panel")
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
        self.query_one("#tracking").border_title = "pipeline"
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
        tracked = self._tracked()
        self.query_one("#tracking", Static).update(
            "not tracked (t to watch)"
            if tracked is None
            else f"{tracked.stage} · pwin {tracked.pwin if tracked.pwin is not None else '-'}"
            f" · updated {tracked.updated_at} (t to change stage)"
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

    def _tracked(self) -> workspace.Tracked | None:
        rows = workspace.pipeline(self.app.conn)
        return next((row for row in rows if row.notice_id == self.notice_id), None)

    def action_track(self) -> None:
        tracked = self._tracked()
        if tracked is None:
            workspace.track(self.app.conn, self.notice_id)
            self.load()
        else:
            self.app.push_screen(StageModal(tracked.stage), self._set_stage)

    def _set_stage(self, stage: str | None) -> None:
        if stage:
            workspace.track(self.app.conn, self.notice_id, stage=stage)
            self.load()

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


class StageModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: str) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="stage_box"):
            yield Label("stage (Enter to set, Escape to cancel)")
            yield ListView(
                *[ListItem(Label(stage), name=stage) for stage in workspace.STAGES], id="stages"
            )

    def on_mount(self) -> None:
        stages = self.query_one("#stages", ListView)
        stages.index = workspace.STAGES.index(self.current)
        stages.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    MODES = {"dashboard": DashboardScreen, "opportunities": OpportunitiesScreen}
    BINDINGS = [
        Binding("1", "switch_mode('dashboard')", "Dashboard"),
        Binding("2", "switch_mode('opportunities')", "Opportunities"),
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
