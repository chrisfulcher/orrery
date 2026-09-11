"""Setup inside the app: connections (written to .env), the profile, the workflow, saved
searches, and jobs. One screen, one tab per concern, Escape to the tab bar, ctrl+s to save."""

from collections.abc import Callable

from pydantic import SecretStr, ValidationError
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Footer,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from orrery import documents, dotenv, jobs, query, workspace
from orrery.config import Settings, env_key, env_values, environment_overrides, setup_needed
from orrery.documents import ProfileDocument, SearchDocument, StageSpec, WorkflowDocument
from orrery.tui.forms import FieldSpec, Form, FormError, flatten, unflatten
from orrery.tui.widgets import WrapTable

LOCKED_ENV = "set by {var} in the environment; the file cannot change it"
LOCKED_STORE = "the store is open on this directory; set ORRERY_DATA_DIR before starting"


def connection_fields(settings: Settings) -> list[FieldSpec]:
    overrides = environment_overrides()

    def spec(key: str, label: str, kind: str = "text", *, empty: object = "", **extra) -> FieldSpec:
        locked = None
        if key in overrides:
            locked = LOCKED_ENV.format(var=env_key(key))
        elif key == "data_dir":
            locked = LOCKED_STORE
        return FieldSpec(key, label, kind, empty=empty, locked=locked, **extra)

    providers = (
        ("OpenAI-compatible (Ollama, LM Studio, OpenAI, ...)", "openai"),
        ("Anthropic", "anthropic"),
    )
    return [
        spec("sam_api_key", "SAM.gov API key", "secret", empty=None, section="SAM.gov"),
        spec("sam_daily_budget", "Keyed requests per day", "int", help="10 without a role"),
        spec("naics", "NAICS codes", "csv", help="comma-separated; the slice everything ingests"),
        spec("sam_base_url", "SAM.gov API base URL"),
        spec("data_dir", "Data directory", section="Store"),
        spec("fetch_delay", "Seconds between attachment downloads", "float"),
        spec("max_attachment_bytes", "Largest attachment to download, bytes", "int"),
        spec("usaspending_base_url", "USAspending API base URL", section="USAspending"),
        spec("embed_base_url", "Embeddings base URL", section="Embeddings"),
        spec("embed_model", "Embedding model"),
        spec("embed_api_key", "Embeddings API key", "secret", empty=None),
        spec("embed_batch_size", "Texts per request", "int"),
        spec("ai_fast_provider", "Provider", "select", options=providers,
             section="Fast model (grunt work)"),
        spec("ai_fast_base_url", "Base URL"),
        spec("ai_fast_model", "Model"),
        spec("ai_fast_api_key", "API key", "secret", empty=None),
        spec("ai_fast_context_chars", "Prompt budget, characters", "int"),
        spec("ai_deep_provider", "Provider", "select", empty=None, options=providers,
             section="Deep model (judgment; blank means the fast model)"),
        spec("ai_deep_base_url", "Base URL", empty=None),
        spec("ai_deep_model", "Model", empty=None, help="blank: claude-opus-5 for Anthropic"),
        spec("ai_deep_api_key", "API key", "secret", empty=None, help="blank: ANTHROPIC_API_KEY"),
        spec("ai_deep_context_chars", "Prompt budget, characters", "int", empty=None),
        spec("ai_timeout", "Seconds to wait for a model response", "float", section="Timeouts"),
    ]  # fmt: skip


class ConnectionsTab(Vertical):
    """Every setting, saved to .env."""

    def compose(self) -> ComposeResult:
        settings = self.app.settings
        values = {f: getattr(settings, f) for f in Settings.model_fields}
        for key in ("sam_api_key", "embed_api_key", "ai_fast_api_key", "ai_deep_api_key"):
            secret = values[key]
            values[key] = secret.get_secret_value() if isinstance(secret, SecretStr) else None
        yield Form(connection_fields(settings), values, id="connections_form")
        yield Static(id="connections_status", classes="status")

    def on_mount(self) -> None:
        self.refresh_status()

    def refresh_status(self) -> None:
        settings = self.app.settings
        env_path = self.app.env_path
        parts = [f"file: {env_path}" if env_path else "no .env path (nothing is written)"]
        parts.append("SAM.gov key set" if settings.sam_api_key else "SAM.gov key missing")
        parts.append(f"NAICS {', '.join(settings.naics) or 'unset'}")
        locked = len(environment_overrides())
        if locked:
            parts.append(f"{locked} value(s) come from the environment")
        self.query_one("#connections_status", Static).update(" · ".join(parts))

    def save(self) -> bool:
        form = self.query_one("#connections_form", Form)
        try:
            edited = form.values()
        except FormError as exc:
            form.show_error(str(exc))
            form.focus_field(exc.key)
            return False
        current = {f: getattr(self.app.settings, f) for f in Settings.model_fields}
        candidate = {**current, **edited}
        for key in ("sam_api_key", "embed_api_key", "ai_fast_api_key", "ai_deep_api_key"):
            value = candidate[key]
            if isinstance(value, SecretStr):
                candidate[key] = value.get_secret_value()
        try:
            new = Settings(_env_file=None, **candidate)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            form.show_error(problems)
            return False
        form.show_error(None)
        env_path = self.app.env_path
        if env_path is None:
            self.app.settings = new
            self.app.notify("settings applied for this session only (no .env path)")
            return True
        overrides = environment_overrides()
        updates = {
            env_key(field): (env_values(new)[env_key(field)] or None)
            for field in Settings.model_fields
            if field not in overrides
        }
        try:
            dotenv.write(env_path, updates)
        except (OSError, ValueError) as exc:
            form.show_error(f"could not write {env_path}: {exc}")
            return False
        self.app.reload_settings()
        self.refresh_status()
        self.app.notify(f"saved {env_path}")
        return True


SIZES = (("small", "small"), ("other than small", "other-than-small"))

PROFILE_FIELDS = [
    FieldSpec("company.name", "Company name", section="Company"),
    FieldSpec("company.uei", "UEI", empty=None, help="12 characters, from SAM.gov"),
    FieldSpec("company.cage", "CAGE", empty=None),
    FieldSpec("offerings.naics", "NAICS codes", "csv", section="Offerings",
              help="comma-separated; the first is the primary"),
    FieldSpec("offerings.psc", "PSC codes", "csv"),
    FieldSpec("offerings.keywords", "Keywords", "csv"),
    FieldSpec("offerings.capability_statement", "Capability statement", "prose"),
    FieldSpec("markets.agency_prefixes", "Target agency path prefixes", "csv",
              section="Markets", help="075 or 075.7526"),
    FieldSpec("markets.office_codes", "Target office codes", "csv", help="75R602"),
    FieldSpec("markets.places", "Places served", "csv"),
    FieldSpec("qualifications.size", "Size", "select", empty=None, options=SIZES,
              section="Qualifications"),
    FieldSpec("qualifications.set_asides", "Set-asides you can bid under", "csv",
              help="SBA, 8A, SDVOSBC, HZC"),
    FieldSpec("qualifications.certifications", "Certifications", "csv"),
    FieldSpec("competitors", "Competitors", "parties", section="Competitors and partners",
              help="one per line: UEI | name | notes"),
    FieldSpec("partners", "Teaming partners", "parties", help="one per line: UEI | name | notes"),
    FieldSpec("ai.notes", "Notes for AI assessments", "prose", section="AI"),
]  # fmt: skip


class ProfileTab(Vertical):
    """The company profile as a form; saved as the same TOML document the CLI edits."""

    def compose(self) -> ComposeResult:
        conn = self.app.conn
        doc = documents.parse(workspace.profile_document(conn), ProfileDocument)
        yield Form(PROFILE_FIELDS, flatten(doc.model_dump()), id="profile_form")
        yield Static(id="profile_status", classes="status")

    def on_mount(self) -> None:
        self.refresh_status()

    def refresh_status(self) -> None:
        latest = workspace.latest_document(self.app.conn, "profile")
        self.query_one("#profile_status", Static).update(
            f"profile v{latest.version}, saved {latest.created_at}"
            if latest
            else "no profile saved yet"
        )

    def save(self) -> bool:
        form = self.query_one("#profile_form", Form)
        try:
            doc = documents.validate(unflatten(form.values()), ProfileDocument)
        except FormError as exc:
            form.show_error(str(exc))
            form.focus_field(exc.key)
            return False
        except documents.DocumentError as exc:
            form.show_error(str(exc))
            form.focus_field(str(exc).split(":", 1)[0])
            return False
        form.show_error(None)
        workspace.save_profile(self.app.conn, documents.render_profile(doc))
        self.refresh_status()
        latest = workspace.latest_document(self.app.conn, "profile")
        self.app.notify(f"saved profile v{latest.version if latest else '?'}")
        return True


STAGE_FIELDS = [
    FieldSpec("key", "Key", help="letters, digits, hyphens; what pursuits store"),
    FieldSpec("name", "Name"),
    FieldSpec("gate", "Gate this stage feeds", empty=None, help="blank for no decision"),
    FieldSpec("tasks", "Starter tasks", "lines", help="one per line"),
]


class FormModal(ModalScreen[dict | None]):
    """A form in a box: ctrl+s validates and returns the values, Escape cancels."""

    BINDINGS = [Binding("ctrl+s", "submit", "Save"), Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        fields: list[FieldSpec],
        values: dict,
        validate: Callable[[dict], dict],
    ) -> None:
        super().__init__()
        self.title_text = title
        self.fields = fields
        self.values = values
        self.validate = validate

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal_box modal_form"):
            yield Label(f"{self.title_text} (ctrl+s to save, Escape to cancel)")
            yield Form(self.fields, self.values, id="modal_form")

    def on_mount(self) -> None:
        self.query_one("#modal_form", Form).focus_field(self.fields[0].key)

    def action_submit(self) -> None:
        form = self.query_one("#modal_form", Form)
        try:
            self.dismiss(self.validate(form.values()))
        except FormError as exc:
            form.show_error(str(exc))
            form.focus_field(exc.key)
        except (documents.DocumentError, ValueError) as exc:
            form.show_error(str(exc))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "keep", "Keep")]

    def __init__(self, question: str, *, verb: str = "delete") -> None:
        super().__init__()
        self.question = question
        self.verb = verb

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal_box"):
            yield Label(self.question)
            yield ListView(
                ListItem(Label(self.verb), name="yes"), ListItem(Label("keep"), name="no"),
                id="confirm",
            )  # fmt: skip

    def on_mount(self) -> None:
        self.query_one("#confirm", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name == "yes")

    def action_keep(self) -> None:
        self.dismiss(False)


class WorkflowTab(Vertical):
    """The stages in order; edit one in a modal; save writes a new workflow version."""

    BINDINGS = [
        Binding("a", "add", "Add stage"),
        Binding("e", "edit", "Edit"),
        Binding("enter", "edit", "Edit", show=False),
        Binding("x", "remove", "Remove"),
        Binding("shift+up", "move(-1)", "Move up"),
        Binding("shift+down", "move(1)", "Move down"),
    ]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.stages: list[StageSpec] = []
        self.dirty = False

    def compose(self) -> ComposeResult:
        yield Static("", id="workflow_errors", classes="errors")
        yield WrapTable(
            [("key", 12), ("name", 24), ("gate", 24), ("starter tasks", None)],
            id="stages",
            classes="panel",
            cursor_type="row",
        )
        yield Static(id="workflow_status", classes="status")

    def on_mount(self) -> None:
        self.stages = list(workspace.workflow(self.app.conn).stages)
        self.dirty = False
        self.refresh_table()

    def refresh_table(self) -> None:
        table = self.query_one("#stages", WrapTable)
        latest = workspace.latest_document(self.app.conn, "workflow")
        version = f"v{latest.version}" if latest else "default"
        table.border_title = f"workflow · {version}" + (" · unsaved (ctrl+s)" if self.dirty else "")
        table.set_rows(
            [
                (
                    (s.key, s.name, s.gate or "-", f"{len(s.tasks)}: {'; '.join(s.tasks)[:120]}"),
                    s.key,
                )
                for s in self.stages
            ]
        )
        self.query_one("#workflow_status", Static).update(
            "a add · e edit · x remove · shift+up/down reorder · ctrl+s save"
        )

    def _selected(self) -> int | None:
        table = self.query_one("#stages", WrapTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return next((i for i, s in enumerate(self.stages) if s.key == key), None)

    def _validate_stage(self, index: int | None) -> Callable[[dict], dict]:
        def validate(values: dict) -> dict:
            spec = documents.validate(values, StageSpec)
            for i, other in enumerate(self.stages):
                if i != index and other.key == spec.key:
                    raise ValueError(f"duplicate stage key {spec.key!r}")
            return spec.model_dump()

        return validate

    def action_add(self) -> None:
        self.app.push_screen(
            FormModal("new stage", STAGE_FIELDS, {"tasks": []}, self._validate_stage(None)),
            lambda result: self._stage_saved(None, result),
        )

    def action_edit(self) -> None:
        index = self._selected()
        if index is None:
            return
        self.app.push_screen(
            FormModal(
                f"stage {self.stages[index].key}",
                STAGE_FIELDS,
                self.stages[index].model_dump(),
                self._validate_stage(index),
            ),  # fmt: skip
            lambda result: self._stage_saved(index, result),
        )

    def _stage_saved(self, index: int | None, result: dict | None) -> None:
        if result is None:
            return
        spec = StageSpec.model_validate(result)
        if index is None:
            self.stages.append(spec)
        else:
            self.stages[index] = spec
        self.dirty = True
        self.refresh_table()

    def action_remove(self) -> None:
        index = self._selected()
        if index is None:
            return
        del self.stages[index]
        self.dirty = True
        self.refresh_table()

    def action_move(self, delta: int) -> None:
        index = self._selected()
        if index is None or not 0 <= index + delta < len(self.stages):
            return
        stages = self.stages
        stages[index], stages[index + delta] = stages[index + delta], stages[index]
        self.dirty = True
        self.refresh_table()
        self.query_one("#stages", WrapTable).move_cursor(row=index + delta)

    def save(self) -> bool:
        errors = self.query_one("#workflow_errors", Static)
        try:
            doc = documents.validate(
                {"stages": [s.model_dump() for s in self.stages]}, WorkflowDocument
            )
            workspace.save_workflow(self.app.conn, documents.render_workflow(doc))
        except (documents.DocumentError, ValueError) as exc:
            errors.update(str(exc))
            errors.set_class(True, "-visible")
            return False
        errors.update("")
        errors.set_class(False, "-visible")
        self.dirty = False
        self.refresh_table()
        self.app.notify("saved workflow")
        return True


SEARCH_FIELDS = [
    FieldSpec("query", "Search text", empty=None, help="FTS5; blank means filters only"),
    FieldSpec("naics", "NAICS codes", "csv"),
    FieldSpec("set_asides", "Set-aside codes", "csv"),
    FieldSpec("agency_prefixes", "Agency path prefixes", "csv"),
    FieldSpec("deadline_within_days", "Deadline within days", "int", empty=None),
]


class SearchesTab(Vertical):
    BINDINGS = [
        Binding("n", "new", "New search"),
        Binding("e", "edit", "Edit"),
        Binding("enter", "edit", "Edit", show=False),
        Binding("x", "delete", "Delete"),
        Binding("r", "run", "Run"),
    ]

    def compose(self) -> ComposeResult:
        yield WrapTable(
            [("name", 20), ("filters", None)],
            id="searches_table",
            classes="panel",
            cursor_type="row",
        )
        yield Static("n new · e edit · x delete · r run", id="searches_status", classes="status")

    def on_mount(self) -> None:
        self.refresh_table()

    def refresh_table(self) -> None:
        rows = workspace.list_searches(self.app.conn)
        table = self.query_one("#searches_table", WrapTable)
        table.border_title = f"saved searches · {len(rows)}"
        table.set_rows([((s.name, workspace.describe_search(s)), s.name) for s in rows])

    def _selected(self) -> str | None:
        table = self.query_one("#searches_table", WrapTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def action_new(self) -> None:
        fields = [FieldSpec("name", "Name", help="unique to you"), *SEARCH_FIELDS]
        self.app.push_screen(
            FormModal("new saved search", fields, {}, self._validate_search),
            lambda result: self._search_saved(None, result),
        )

    def action_edit(self) -> None:
        name = self._selected()
        if name is None:
            return
        saved = workspace.get_search(self.app.conn, name)
        values = {
            "query": saved.query,
            "naics": list(saved.filters.naics or ()),
            "set_asides": list(saved.filters.set_asides or ()),
            "agency_prefixes": list(saved.filters.agency_prefixes or ()),
            "deadline_within_days": saved.filters.deadline_within_days,
        }
        self.app.push_screen(
            FormModal(f"search {name}", SEARCH_FIELDS, values, self._validate_search),
            lambda result: self._search_saved(name, result),
        )

    @staticmethod
    def _validate_search(values: dict) -> dict:
        name = values.pop("name", None)
        doc = documents.validate(values, SearchDocument)
        if name is not None and not name.strip():
            raise ValueError("name: a search needs a name")
        return {"name": name, **doc.model_dump()}

    def _search_saved(self, name: str | None, result: dict | None) -> None:
        if result is None:
            return
        name = name or result["name"].strip()
        doc = SearchDocument.model_validate({k: v for k, v in result.items() if k != "name"})
        workspace.save_search_document(self.app.conn, name, documents.render_search(name, doc))
        self.refresh_table()
        self.app.notify(f"saved search {name}")

    def action_delete(self) -> None:
        name = self._selected()
        if name is None:
            return
        self.app.push_screen(
            ConfirmModal(f"delete saved search {name!r}?"),
            lambda yes: self._deleted(name, yes),
        )

    def _deleted(self, name: str, yes: bool | None) -> None:
        if yes:
            workspace.delete_search(self.app.conn, name)
            self.refresh_table()
            self.app.notify(f"deleted {name}")

    def action_run(self) -> None:
        name = self._selected()
        if name is None:
            return
        hits = workspace.run_search(self.app.conn, name, limit=200)
        self.app.run_worker(self.app.show_hits(hits, f"saved search {name}"), exclusive=False)


JOB_SOURCES = {
    "ingest-notices": "sam_opportunities_api",
    "ingest-bulk": "sam_bulk_csv",
    "ingest-awards": "usaspending_awards",
    "ingest-entities": "sam_entities",
    "fetch": "sam_opportunities_api",
    "sync": "sam_bulk_csv",
}


def param_fields(job: jobs.Job) -> list[FieldSpec]:
    fields = []
    for param in job.params:
        kind = {"date": "text", "int": "int", "str": "text", "csv": "csv", "choice": "select",
                "bool": "select"}[param.kind]  # fmt: skip
        options = tuple((c, c) for c in param.choices)
        if param.kind == "bool":
            options = (("yes", "yes"), ("no", "no"))
        fields.append(
            FieldSpec(
                param.name,
                param.label,
                kind,
                empty=None,
                options=options,
                help=param.help + (" (required)" if param.required else ""),
            )  # fmt: skip
        )
    return fields


class JobsTab(Vertical):
    BINDINGS = [
        Binding("r", "run", "Run"),
        Binding("enter", "run", "Run", show=False),
        Binding("c", "cancel", "Cancel"),
        Binding("l", "focus_log", "Log"),
    ]

    def compose(self) -> ComposeResult:
        yield WrapTable(
            [("operation", 34), ("needs", 22), ("last run", 22), ("status", None)],
            id="operations",
            classes="panel",
            cursor_type="row",
        )
        yield RichLog(id="job_log", classes="panel", wrap=True, markup=False, max_lines=2000)

    def on_mount(self) -> None:
        self.shown = 0
        self.refresh_jobs()
        self.set_interval(1.0, self.refresh_jobs)

    def refresh_jobs(self) -> None:
        app = self.app
        last_runs = query.last_runs(app.conn)
        current = app.current
        rows = []
        for name in jobs.OPERATIONS:
            job = jobs.JOBS[name]
            needs = ", ".join(sorted(job.needs)) or "-"
            ref = last_runs.get(JOB_SOURCES.get(name, ""))
            last = f"{ref.started_at[:16]} {ref.status}" if ref else "-"
            status = ""
            if current and current.job.name == name:
                status = (
                    f"{current.state}: {current.last_line}" if current.last_line else current.state
                )
            else:
                done = next((r for r in reversed(app.history) if r.job.name == name), None)
                if done:
                    status = f"{done.state}: {done.last_line}"
            rows.append(((job.label, needs, last, status), name))
        table = self.query_one("#operations", WrapTable)
        table.border_title = (
            f"operations · {current.job.label} {current.state}"
            if current and current.finished_at is None
            else "operations · idle · r runs the selected one"
        )
        table.set_rows(rows)
        log = self.query_one("#job_log", RichLog)
        if current is None:
            log.border_title = "log"
            return
        log.border_title = f"log · {current.job.label} · {current.state}"
        if getattr(self, "logged_run", None) is not current:
            log.clear()
            self.logged_run = current
            self.shown = 0
        for line in list(current.lines)[self.shown :]:
            log.write(line)
        self.shown = len(current.lines)

    def _selected(self) -> str | None:
        table = self.query_one("#operations", WrapTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def action_run(self) -> None:
        name = self._selected()
        if name is None:
            return
        job = jobs.JOBS[name]
        if not job.params:
            self.app.run_job(name, {})
            return
        defaults = {p.name: p.default for p in job.params if p.default is not None}
        self.app.push_screen(
            FormModal(
                job.label, param_fields(job), defaults, lambda values: jobs.coerce(job, values)
            ),
            lambda values: values is not None and self.app.run_job(name, values),
        )

    def action_cancel(self) -> None:
        self.app.action_cancel_job()

    def action_focus_log(self) -> None:
        self.query_one("#job_log", RichLog).focus()


PROBE_BY_PREFIX = (
    ("sam_", "probe-sam"),
    ("naics", "probe-sam"),
    ("embed_", "probe-embed"),
    ("ai_fast_", "probe-fast"),
    ("ai_deep_", "probe-deep"),
    ("ai_timeout", "probe-deep"),
)


class SetupScreen(Screen):
    BINDINGS = [
        Binding("escape", "focus_tabs", "Tabs"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+t", "test", "Test connection"),
        Binding("enter", "enter_tab", "Into the tab", show=False),
    ]

    def __init__(self, initial: str = "connections") -> None:
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        yield Static("", id="setup_banner")
        with TabbedContent(initial=self.initial, id="setup_tabs"):
            with TabPane("Connections", id="connections"):
                yield ConnectionsTab(id="connections_tab")
            with TabPane("Profile", id="profile"):
                yield ProfileTab(id="profile_tab")
            with TabPane("Workflow", id="workflow"):
                yield WorkflowTab(id="workflow_tab")
            with TabPane("Searches", id="searches"):
                yield SearchesTab(id="searches_tab")
            with TabPane("Jobs", id="jobs"):
                yield JobsTab(id="jobs_tab")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_banner()

    def on_screen_resume(self) -> None:
        self.refresh_banner()

    def refresh_banner(self) -> None:
        reason = setup_needed(self.app.settings, self.app.env_path)
        banner = self.query_one("#setup_banner", Static)
        banner.update(
            f"first-run setup: {reason}. Enter the SAM.gov key and NAICS codes, then ctrl+s."
            if reason
            else ""
        )
        banner.set_class(bool(reason), "-visible")

    TABLES = {"workflow": "#stages", "searches": "#searches_table", "jobs": "#operations"}
    """Tabs whose keys live on a table, which takes focus when the tab is shown."""

    def show_tab(self, pane: str) -> None:
        self.query_one("#setup_tabs", TabbedContent).active = pane
        self._focus_table(pane)

    def action_enter_tab(self) -> None:
        """From the tab bar, Enter steps into the active tab: its table, or its first field."""
        self._focus_table(self.query_one("#setup_tabs", TabbedContent).active)

    def _focus_table(self, pane: str) -> None:
        selector = self.TABLES.get(pane)
        if selector is not None:
            self.query_one(selector, WrapTable).focus()
            return
        pane = self.query_one("#setup_tabs", TabbedContent).get_pane(pane)
        fields = pane.query("Input, TextArea, Select")
        if fields:
            fields.first().focus()

    def action_focus_tabs(self) -> None:
        self.query_one(Tabs).focus()

    def refresh_jobs(self) -> None:
        try:
            self.query_one("#jobs_tab", JobsTab).refresh_jobs()
        except Exception:  # the tab is not mounted yet
            return

    def action_test(self) -> None:
        """Probe the service the focused Connections field belongs to."""
        if self.query_one("#setup_tabs", TabbedContent).active != "connections":
            self.notify("tests run from the Connections tab", severity="warning")
            return
        focused = self.app.focused
        key = (focused.id or "").removeprefix("field-").replace("-", "_") if focused else ""
        probe = next((name for prefix, name in PROBE_BY_PREFIX if key.startswith(prefix)), None)
        if probe is None:
            self.notify("put the cursor in a SAM.gov, embeddings, or model field first")
            return
        status = self.query_one("#connections_status", Static)

        def done() -> None:
            run = self.app.history[-1] if self.app.history else None
            if run is not None:
                status.update(run.last_line)

        if probe == "probe-sam":
            self.app.push_screen(
                ConfirmModal(
                    "test the SAM.gov key? This spends one of today's requests.", verb="test"
                ),
                lambda yes: yes and self.app.run_job(probe, {}, on_done=done),
            )
        else:
            self.app.run_job(probe, {}, on_done=done)

    def action_save(self) -> None:
        active = self.query_one("#setup_tabs", TabbedContent).active
        if active == "connections":
            if self.query_one("#connections_tab", ConnectionsTab).save():
                self.refresh_banner()
        elif active == "profile":
            self.query_one("#profile_tab", ProfileTab).save()
        elif active == "workflow":
            self.query_one("#workflow_tab", WorkflowTab).save()
        else:
            self.notify("nothing to save on this tab yet", severity="warning")
