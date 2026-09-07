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
            yield DataTable(id="deadlines", classes="panel", cursor_type="row")
            yield DataTable(id="pipeline", classes="panel", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#quota").border_title = "quota"
        self.query_one("#queues").border_title = "queues"
        self.query_one("#activity").border_title = "activity"
        deadlines = self.query_one("#deadlines", DataTable)
        deadlines.border_title = "deadlines, next 7 days"
        deadlines.add_columns("deadline", "agency", "title")
        pipeline = self.query_one("#pipeline", DataTable)
        pipeline.border_title = "pipeline"
        pipeline.add_columns("deadline", "stage", "pwin", "title")
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
        deadlines = self.query_one("#deadlines", DataTable)
        deadlines.clear()
        for hit in query.upcoming(conn, days=7, limit=20):
            deadlines.add_row(
                _day(hit.response_deadline),
                (hit.agency or "-")[:28],
                hit.title[:70],
                key=hit.notice_id,
            )
        pipeline = self.query_one("#pipeline", DataTable)
        pipeline.clear()
        rows = workspace.pipeline(conn)
        by_stage = Counter(row.stage for row in rows)
        summary = ", ".join(
            f"{stage} {by_stage[stage]}" for stage in workspace.STAGES if by_stage[stage]
        )
        pipeline.border_title = f"pipeline · {summary}" if summary else "pipeline · nothing tracked"
        for tracked in rows[:10]:
            pipeline.add_row(
                _day(tracked.response_deadline),
                tracked.stage,
                "-" if tracked.pwin is None else str(tracked.pwin),
                tracked.title[:40],
                key=tracked.notice_id,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.app.push_screen(ContextScreen(event.row_key.value))


class OpportunitiesScreen(Screen):
    BINDINGS = [Binding("escape", "focus_table", "Table", show=False)]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search notice and attachment text, Enter to run", id="query")
        yield DataTable(id="hits", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#hits", DataTable)
        table.add_columns("deadline", "agency", "title", "source")
        self.fill(query.upcoming(self.app.conn, days=30, limit=200))
        table.focus()

    def fill(self, hits: list[query.SearchHit]) -> None:
        table = self.query_one("#hits", DataTable)
        table.clear()
        for hit in hits:
            table.add_row(
                _day(hit.response_deadline),
                (hit.agency or "-")[:28],
                hit.title[:70],
                hit.source[:24],
                key=hit.notice_id,
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
    """The context view: one notice, its agency chain, text, documents, and tracking."""

    BINDINGS = [
        Binding("t", "track", "Track / stage"),
        Binding("a", "agency", "Agency"),
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
        with VerticalScroll(id="description_scroll", classes="panel"):
            yield Static(id="description")
        yield DataTable(id="attachments", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#header").border_title = "notice"
        self.query_one("#tracking").border_title = "pipeline"
        self.query_one("#description_scroll").border_title = "description"
        attachments = self.query_one("#attachments", DataTable)
        attachments.border_title = "documents"
        attachments.add_columns("file", "fetch", "extract", "chars")
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
        self.query_one("#description", Static).update(
            detail.description or f"description {detail.description_status}"
        )
        attachments = self.query_one("#attachments", DataTable)
        attachments.clear()
        for item in detail.attachments:
            attachments.add_row(
                (item.filename or item.url.rsplit("/", 2)[-2])[:50],
                item.fetch_status,
                item.extract_status,
                str(item.text_chars),
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

    def action_open(self) -> None:
        if self.detail and self.detail.url:
            webbrowser.open(self.detail.url)


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

    def compose(self) -> ComposeResult:
        yield Static(id="entity_header", classes="panel")
        yield DataTable(id="entity_notices", classes="panel", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        detail = query.entity(self.app.conn, self.entity_id, recent=50)
        header = self.query_one("#entity_header", Static)
        header.border_title = "entity"
        table = self.query_one("#entity_notices", DataTable)
        table.border_title = "recent notices"
        table.add_columns("deadline", "agency", "title")
        if detail is None:
            header.update(f"no entity {self.entity_id}")
            return
        children = ", ".join(child.name for child in detail.children) or "-"
        header.update(
            f"{detail.kind}: {detail.name}\n"
            f"{' › '.join(ref.name for ref in detail.chain)}\n"
            f"path {detail.path_code or '-'} · {detail.notices} notice(s)\n"
            f"offices: {children}"
        )
        for hit in detail.recent:
            table.add_row(
                _day(hit.response_deadline),
                (hit.agency or "-")[:28],
                hit.title[:70],
                key=hit.notice_id,
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self.app.push_screen(ContextScreen(event.row_key.value))


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
