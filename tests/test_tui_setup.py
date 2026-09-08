import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.widgets import Input, Select, Static, TabbedContent, TextArea

from mentor import db, documents, dotenv, workspace
from mentor.config import Settings, env_values
from mentor.documents import ProfileDocument
from mentor.tui.app import DashboardScreen, MentorTop, RadarScreen
from mentor.tui.setup import SetupScreen

Seed = Callable[[dict | None], None]


def text(app: MentorTop, selector: str) -> str:
    return str(app.screen.query_one(selector, Static).content)


async def test_first_run_opens_setup_and_saves_the_env_file(
    conn: sqlite3.Connection, seed: Seed, tmp_path: Path
) -> None:
    seed()
    conn.close()
    bare = Settings(_env_file=None, data_dir=tmp_path)  # no key, no NAICS
    env_path = tmp_path / ".env"
    app = MentorTop(bare, env_path=env_path)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        assert "MENTOR_SAM_API_KEY is not set" in text(app, "#setup_banner")

        app.screen.query_one("#field-sam-api-key", Input).value = "sk-live"
        app.screen.query_one("#field-naics", Input).value = "541512, 541511"
        app.screen.query_one("#field-sam-daily-budget", Input).value = "1000"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert env_path.is_file() and stat.S_IMODE(env_path.stat().st_mode) == 0o600
        stored = dotenv.read(env_path)
        assert stored["MENTOR_SAM_API_KEY"] == "sk-live"
        assert stored["MENTOR_NAICS"] == "541512,541511"
        assert stored["MENTOR_DATA_DIR"] == str(tmp_path)  # the locked value is carried, not lost
        assert app.settings.naics == ["541512", "541511"] and app.settings.sam_daily_budget == 1000
        assert app.settings.sam_api_key.get_secret_value() == "sk-live"
        assert text(app, "#setup_banner") == ""
        assert "SAM.gov key set" in text(app, "#connections_status")

        await pilot.press("escape")  # to the tab bar, so the mode keys work again
        await pilot.press("1")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert "spent 1 of 1000" in text(app, "#quota_text")
        await pilot.press("q")


async def test_environment_values_are_locked_and_bad_input_writes_nothing(
    conn: sqlite3.Connection,
    settings: Settings,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed()
    conn.close()
    env_path = tmp_path / ".env"
    dotenv.write(env_path, env_values(settings))
    before = env_path.read_text()
    monkeypatch.setenv("MENTOR_SAM_DAILY_BUDGET", "99")
    app = MentorTop(Settings(_env_file=env_path), env_path=env_path)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("5")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        budget = app.screen.query_one("#field-sam-daily-budget", Input)
        assert budget.disabled and budget.value == "99"
        assert "1 value(s) come from the environment" in text(app, "#connections_status")

        app.screen.query_one("#field-fetch-delay", Input).value = "fast"
        await pilot.press("ctrl+s")
        await pilot.pause()
        errors = text(app, "#connections_form-errors")
        assert "Seconds between attachment downloads must be a number" in errors
        assert env_path.read_text() == before

        app.screen.query_one("#field-fetch-delay", Input).value = "2.5"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert text(app, "#connections_form-errors") == ""
        stored = dotenv.read(env_path)
        assert stored["MENTOR_FETCH_DELAY"] == "2.5"
        assert stored["MENTOR_SAM_DAILY_BUDGET"] == "10"  # the locked line is left as it was
        assert app.settings.fetch_delay == 2.5 and app.settings.sam_daily_budget == 99

        await pilot.press("escape")
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.query_one("#setup_tabs", TabbedContent).active == "profile"
        await pilot.press("q")


async def test_migrations_run_on_start(settings: Settings, tmp_path: Path) -> None:
    raw = db.connect(settings.db_path)  # a store that has never been migrated
    assert db.status(raw).applied == []
    raw.close()
    env_path = tmp_path / ".env"
    dotenv.write(env_path, env_values(settings))
    app = MentorTop(settings, env_path=env_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("q")
    check = db.connect(settings.db_path)
    assert db.status(check).pending == []
    check.close()


@pytest.fixture
def ready_app(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> MentorTop:
    seed()
    conn.close()
    env_path = tmp_path / ".env"
    dotenv.write(env_path, env_values(settings))
    return MentorTop(settings, env_path=env_path)


async def test_profile_form_round_trips_the_document(
    ready_app: MentorTop, settings: Settings
) -> None:
    app = ready_app
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.press("5")
        await pilot.pause()
        app.screen.show_tab("profile")
        await pilot.pause()
        assert text(app, "#profile_status") == "no profile saved yet"
        screen = app.screen
        screen.query_one("#field-company-name", Input).value = "Example LLC"
        screen.query_one("#field-company-uei", Input).value = "ue9qjd4kk1l6"
        screen.query_one("#field-offerings-naics", Input).value = "541512, 541511"
        screen.query_one(
            "#field-offerings-capability-statement", TextArea
        ).text = "Help desks.\nZero trust."
        screen.query_one("#field-qualifications-size", Select).value = "small"
        screen.query_one("#field-qualifications-set-asides", Input).value = "SBA"
        screen.query_one(
            "#field-competitors", TextArea
        ).text = "PHZDZ8SJ5CM1 | CDW GOVERNMENT LLC | incumbent at HRSA\n | Partner Co |"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert text(app, "#profile_form-errors") == ""
        assert text(app, "#profile_status").startswith("profile v1, saved ")

        conn = db.connect(settings.db_path)
        latest = workspace.latest_document(conn, "profile")
        doc = documents.parse(latest.body, ProfileDocument)
        assert doc.company.name == "Example LLC" and doc.company.uei == "UE9QJD4KK1L6"
        assert doc.offerings.naics == ["541512", "541511"]
        assert doc.offerings.capability_statement == "Help desks.\nZero trust."
        assert doc.qualifications.size == "small" and doc.qualifications.set_asides == ["SBA"]
        assert [(c.uei, c.name, c.notes) for c in doc.competitors] == [
            ("PHZDZ8SJ5CM1", "CDW GOVERNMENT LLC", "incumbent at HRSA"),
            (None, "Partner Co", ""),
        ]
        assert latest.body.startswith("# mentor company profile")  # the CLI sees the same document
        assert workspace.get_profile(conn).naics == ("541512", "541511")
        conn.close()

        screen.query_one("#field-company-uei", Input).value = "short"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "company.uei: " in text(app, "#profile_form-errors")
        assert text(app, "#profile_status").startswith("profile v1,")  # nothing new saved

        await pilot.press("escape")
        await pilot.press("4")
        await pilot.pause()
        assert isinstance(app.screen, RadarScreen)
        assert "NAICS 541512, 541511" in app.screen.query_one("#radar").border_title
        await pilot.press("q")


@pytest.fixture
def app_with_pursuit(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path
) -> MentorTop:
    seed()
    hrsa = __import__("conftest").SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"]
    workspace.track(conn, hrsa, stage="pursuing")  # a pursuit in qualify
    workspace.save_search(
        conn, "sba", filters=__import__("mentor.query").query.Filters(set_asides=("SBA",))
    )
    conn.close()
    env_path = tmp_path / ".env"
    dotenv.write(env_path, env_values(settings))
    return MentorTop(settings, env_path=env_path)


async def test_workflow_tab_edits_stages_and_refuses_removing_one_in_use(
    app_with_pursuit: MentorTop, settings: Settings
) -> None:
    from mentor.tui.setup import FormModal

    app = app_with_pursuit
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.press("5")
        await pilot.pause()
        app.screen.show_tab("workflow")
        await pilot.pause()
        table = app.screen.query_one("#stages")
        assert table.row_count == 6 and "default" in table.border_title
        table.focus()

        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, FormModal)
        app.screen.query_one("#field-key", Input).value = "identify"  # duplicate
        app.screen.query_one("#field-name", Input).value = "Again"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, FormModal)
        assert "duplicate stage key 'identify'" in text(app, "#modal_form-errors")
        app.screen.query_one("#field-key", Input).value = "review"
        app.screen.query_one("#field-name", Input).value = "Review"
        app.screen.query_one("#field-tasks", TextArea).text = "Check\nDouble check"
        await pilot.press("ctrl+s")
        await pilot.pause()
        table = app.screen.query_one("#stages")
        assert table.row_count == 7 and "unsaved" in table.border_title
        assert table.get_row_at(6)[3] == "2: Check; Double check"

        table.move_cursor(row=6)
        await pilot.press("shift+up")
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "unsaved" not in app.screen.query_one("#stages").border_title
        conn = db.connect(settings.db_path)
        assert workspace.workflow(conn).keys() == [
            "identify", "qualify", "capture", "proposal", "submitted", "review", "post-award",
        ]  # fmt: skip
        conn.close()

        table = app.screen.query_one("#stages")
        table.move_cursor(row=1)  # qualify, in use by the pursuit
        await pilot.press("x")
        await pilot.pause()
        assert table.row_count == 6
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "in use by open pursuits cannot be removed: ['qualify']" in text(
            app, "#workflow_errors"
        )
        await pilot.press("q")


async def test_searches_tab_creates_edits_runs_and_deletes(
    app_with_pursuit: MentorTop, settings: Settings
) -> None:
    from mentor.tui.app import OpportunitiesScreen
    from mentor.tui.setup import ConfirmModal, FormModal

    app = app_with_pursuit
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.press("5")
        await pilot.pause()
        app.screen.show_tab("searches")
        await pilot.pause()
        table = app.screen.query_one("#searches_table")
        assert table.row_count == 1 and table.get_row_at(0)[1] == "set_asides=SBA"
        table.focus()

        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, FormModal)
        app.screen.query_one("#field-name", Input).value = "it"
        app.screen.query_one("#field-naics", Input).value = "541512"
        app.screen.query_one("#field-deadline-within-days", Input).value = "30"
        await pilot.press("ctrl+s")
        await pilot.pause()
        table = app.screen.query_one("#searches_table")
        assert (
            table.row_count == 2
            and table.get_row_at(0)[1] == "naics=541512 deadline_within_days=30"
        )

        table.move_cursor(row=1)  # sba
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, FormModal)
        app.screen.query_one("#field-query", Input).value = "Microsoft"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert (
            app.screen.query_one("#searches_table").get_row_at(1)[1]
            == 'query="Microsoft" set_asides=SBA'
        )

        await pilot.press("r")
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, OpportunitiesScreen)
        hits = app.screen.query_one("#hits")
        assert hits.row_count >= 1 and hits.border_title == "saved search sba"

        await pilot.press("5")
        await pilot.pause()
        app.screen.query_one("#searches_table").focus()
        await pilot.press("x")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.query_one("#searches_table").row_count == 2
        await pilot.press("x")
        await pilot.press("enter")  # "delete" is the first item
        await pilot.pause()
        assert app.screen.query_one("#searches_table").row_count == 1
        conn = db.connect(settings.db_path)
        assert [s.name for s in workspace.list_searches(conn)] == ["it"]
        conn.close()
        await pilot.press("q")
