import os
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


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("MENTOR_"):
            monkeypatch.delenv(name)


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
