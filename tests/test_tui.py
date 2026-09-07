import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SEARCH_FIXTURE
from textual.widgets import DataTable, Static

from mentor import db, workspace
from mentor.config import Settings
from mentor.tui.app import (
    ContextScreen,
    DashboardScreen,
    EntityScreen,
    MentorTop,
    OpportunitiesScreen,
)

Seed = Callable[[dict | None], None]
HRSA = SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"]


@pytest.fixture
def app(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> MentorTop:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-06T00:00:00Z")
    seed()
    workspace.track(conn, HRSA, stage="pursuing", pwin=40)
    conn.close()
    return MentorTop(settings)


def text(app: MentorTop, selector: str) -> str:
    return str(app.screen.query_one(selector, Static).content)


async def test_dashboard_search_context_and_entity(app: MentorTop) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert "5 notices, 5 active" in text(app, "#activity_text")
        assert "spent 1 of 10" in text(app, "#quota_text")
        assert app.screen.query_one("#deadlines", DataTable).row_count == 2
        assert (
            app.screen.query_one("#pipeline", DataTable).row_count == 1
        )  # one stage row, one notice

        await pilot.press("2")
        await pilot.pause()
        assert isinstance(app.screen, OpportunitiesScreen)
        assert app.screen.query_one("#hits", DataTable).row_count == 4

        await pilot.press("slash")
        await pilot.press(*"Microsoft")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#hits", DataTable).row_count == 2

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ContextScreen)
        assert "Microsoft" in text(app, "#header")
        assert "not tracked" in text(app, "#tracking")

        await pilot.press("t")
        await pilot.pause()
        assert text(app, "#tracking").startswith("watching")

        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, EntityScreen)
        assert "office:" in text(app, "#entity_header")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ContextScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, OpportunitiesScreen)

        await pilot.press("1")
        await pilot.pause()
        assert (
            app.screen.query_one("#pipeline", DataTable).row_count == 2
        )  # two stages, two notices

        await pilot.press("q")
    assert not app.is_running


async def test_screenshot(app: MentorTop, tmp_path: Path) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        saved = app.save_screenshot("top.svg", path=str(tmp_path))
    assert Path(saved).stat().st_size > 1000
    print(f"screenshot: {saved}")
