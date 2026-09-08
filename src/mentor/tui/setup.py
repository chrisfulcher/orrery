"""Setup inside the app: connections (written to .env), the profile, the workflow, saved
searches, and jobs. One screen, one tab per concern, Escape to the tab bar, ctrl+s to save."""

from pydantic import SecretStr, ValidationError
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Static, TabbedContent, TabPane, Tabs

from mentor import dotenv
from mentor.config import Settings, env_key, env_values, environment_overrides, setup_needed
from mentor.tui.forms import FieldSpec, Form, FormError

LOCKED_ENV = "set by {var} in the environment; the file cannot change it"
LOCKED_STORE = "the store is open on this directory; set MENTOR_DATA_DIR before starting"


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


class SetupScreen(Screen):
    BINDINGS = [
        Binding("escape", "focus_tabs", "Tabs"),
        Binding("ctrl+s", "save", "Save"),
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
                yield Static("profile form arrives in the next commit", classes="placeholder")
            with TabPane("Workflow", id="workflow"):
                yield Static("workflow editor arrives soon", classes="placeholder")
            with TabPane("Searches", id="searches"):
                yield Static("saved searches arrive soon", classes="placeholder")
            with TabPane("Jobs", id="jobs"):
                yield Static("jobs arrive soon", classes="placeholder")
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

    def show_tab(self, pane: str) -> None:
        self.query_one("#setup_tabs", TabbedContent).active = pane

    def action_focus_tabs(self) -> None:
        self.query_one(Tabs).focus()

    def action_save(self) -> None:
        active = self.query_one("#setup_tabs", TabbedContent).active
        if active == "connections":
            if self.query_one("#connections_tab", ConnectionsTab).save():
                self.refresh_banner()
        else:
            self.notify("nothing to save on this tab yet", severity="warning")
