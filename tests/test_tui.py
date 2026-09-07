import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import SEARCH_FIXTURE
from textual.widgets import DataTable, Static

from mentor import db, workspace
from mentor.config import Settings
from mentor.ingest.awards import AwardsResult
from mentor.tui.app import (
    ContextScreen,
    DashboardScreen,
    EntityScreen,
    MentorTop,
    OpportunitiesScreen,
    PursueModal,
    PursuitScreen,
    PursuitsScreen,
    RadarScreen,
)

Seed = Callable[[dict | None], None]
SeedAwards = Callable[[list[dict] | None], AwardsResult]
HRSA = SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"]


@pytest.fixture
def app(
    conn: sqlite3.Connection, settings: Settings, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> MentorTop:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-06T00:00:00Z")
    seed()
    workspace.track(conn, HRSA, stage="pursuing", pwin=40)
    pursued = workspace.pursuit_for_notice(conn, HRSA)
    assert pursued is not None
    workspace.add_task(conn, pursued.pursuit_id, "Call the COR", due="2026-09-07")
    workspace.add_task(conn, pursued.pursuit_id, "Draft the capture plan", due="2026-09-08")
    workspace.save_assessment(
        conn, pursued.pursuit_id, slot="deep", provider="openai", model="qwen3:14b",
        prompt_version=1, profile_version=None, inputs_hash="h", input_tokens=900,
        output_tokens=120, raw_response="{}",
        result={
            "fit": 72, "fit_reasons": ["NAICS matches"], "gaps": [], "incumbent_standing": "-",
            "competitive_picture": "-", "decision": "go", "decision_why": "fits",
            "open_questions": [],
            "suggested_tasks": [
                {"title": "Call the COR", "stage": "qualify"},  # already a task
                {"title": "Map the buying office", "stage": "qualify"},
                {"title": "Price to win", "stage": "capture"},
            ],
        },
    )  # fmt: skip
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
        work = app.screen.query_one("#work", DataTable)
        assert work.row_count == 2 and "2 item(s)" in work.border_title
        assert app.screen.query_one("#attention", DataTable).row_count == 0
        assert "qualify 1" in app.screen.query_one("#attention", DataTable).border_title

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
        assert "not pursued" in text(app, "#pursuit")

        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, PursueModal)
        await pilot.press("enter")  # new pursuit from this notice
        await pilot.pause()
        assert isinstance(app.screen, PursuitScreen)
        assert "identify → Pursuit Gate" in text(app, "#pursuit_header")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ContextScreen)
        assert text(app, "#pursuit").startswith("#2 ") and "identify" in text(app, "#pursuit")

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

        await pilot.press("3")
        await pilot.pause()
        assert isinstance(app.screen, PursuitsScreen)
        board = app.screen.query_one("#board", DataTable)
        assert board.row_count == 2 and "identify 1, qualify 1" in board.border_title

        await pilot.press("q")
    assert not app.is_running


async def test_dashboard_refresh_keeps_the_selected_row(app: MentorTop) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        work = app.screen.query_one("#work", DataTable)
        work.focus()
        await pilot.press("down")
        await pilot.pause()
        assert work.cursor_row == 1
        selected = work.coordinate_to_cell_key(work.cursor_coordinate).row_key.value

        app.screen.refresh_panels()  # what the five-second timer does
        await pilot.pause()
        assert work.cursor_row == 1
        assert work.coordinate_to_cell_key(work.cursor_coordinate).row_key.value == selected

        await pilot.resize_terminal(100, 40)  # a rebuild with new widths keeps the row too
        await pilot.pause()
        assert work.coordinate_to_cell_key(work.cursor_coordinate).row_key.value == selected


async def test_screenshot(app: MentorTop, tmp_path: Path) -> None:
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        saved = app.save_screenshot("top.svg", path=str(tmp_path))
    assert Path(saved).stat().st_size > 1000
    print(f"screenshot: {saved}")


@pytest.fixture
def app_with_awards(
    conn: sqlite3.Connection,
    settings: Settings,
    seed_awards: SeedAwards,
    monkeypatch: pytest.MonkeyPatch,
) -> MentorTop:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-06T00:00:00Z")
    seed_awards()
    conn.close()
    return MentorTop(settings)


async def test_context_view_awards_panels_and_contractor_screen(app_with_awards: MentorTop) -> None:
    app = app_with_awards
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.push_screen(ContextScreen(HRSA))
        await pilot.pause()
        assert isinstance(app.screen, ContextScreen)
        assert text(app, "#incumbent").startswith("LEIDOS, INC. · 75R60222F00009 · $125,000")
        awards = app.screen.query_one("#awards", DataTable)
        assert awards.row_count == 2 and "HRSA HEADQUARTERS" in awards.border_title
        assert text(app, "#officials").startswith(
            "Point of Contact 1 (primary) · poc1@example.gov · - · ROCKVILLE MD 20852 · 0 other"
        )

        await pilot.press("i")
        await pilot.pause()
        assert isinstance(app.screen, EntityScreen)
        header = text(app, "#entity_header")
        assert header.startswith("contractor: LEIDOS, INC.")
        assert "uei UE9QJD4KK1L6 · cage 5UTE1 · 1 award(s), $125,000" in header
        assert app.screen.query_one("#entity_awards", DataTable).row_count == 1

        await pilot.press("enter")  # the award row opens the awarding office
        await pilot.pause()
        assert isinstance(app.screen, EntityScreen)
        header = text(app, "#entity_header")
        assert header.startswith("office: HRSA HEADQUARTERS") and "2 award(s), $173,000" in header
        assert app.screen.query_one("#entity_awards", DataTable).row_count == 2

        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ContextScreen)
        army = SEARCH_FIXTURE["opportunitiesData"][1]["noticeId"]
        app.push_screen(ContextScreen(army))
        await pilot.pause()
        assert text(app, "#incumbent").startswith("none known")
        assert "none in the store" in app.screen.query_one("#awards", DataTable).border_title


async def test_pursuit_screen_drives_the_lifecycle(app: MentorTop) -> None:
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.push_screen(PursuitScreen(1))
        await pilot.pause()
        header = text(app, "#pursuit_header")
        assert "qualify → Capture Gate" in header and "pwin 40" in header
        tasks = app.screen.query_one("#tasks", DataTable)
        assert tasks.row_count == 9  # identify and qualify templates plus the two dated ones

        await pilot.press("d")  # the selected task is done
        await pilot.pause()
        tasks = app.screen.query_one("#tasks", DataTable)
        assert tasks.get_row_at(tasks.row_count - 1)[0] == "x"  # done tasks sort last

        await pilot.press("p")
        await pilot.press(*"60")
        await pilot.press("enter")
        await pilot.pause()
        assert "pwin 60" in text(app, "#pursuit_header")

        await pilot.press("t")
        await pilot.press(*"Price to win")
        await pilot.press("enter")
        await pilot.press(*"2026-09-09")
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.query_one("#tasks", DataTable).row_count == 10

        await pilot.press("g")  # go, with a reason
        await pilot.press("enter")
        await pilot.press(*"fits the profile")
        await pilot.press("enter")
        await pilot.pause()
        assert "capture → Bid Gate" in text(app, "#pursuit_header")
        events = app.screen.query_one("#events", DataTable)
        assert events.get_row_at(0)[1] == "stage" and "(Capture Gate)" in events.get_row_at(0)[2]

        await pilot.press("escape")  # back to the dashboard, which refreshes on resume
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert app.screen.query_one("#work", DataTable).row_count == 2  # one done, one added


async def test_radar_starts_a_pursuit_from_an_award(
    app_with_awards: MentorTop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-06T00:00:00Z")
    app = app_with_awards
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        assert isinstance(app.screen, RadarScreen)
        radar = app.screen.query_one("#radar", DataTable)
        assert radar.row_count == 2 and "next 18 months" in radar.border_title
        assert radar.get_row_at(0)[0] == "2027-02-28"

        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, PursuitScreen)
        header = text(app, "#pursuit_header")
        assert header.startswith("#1 Recompete: 75R60222F00009 · LEIDOS, INC.")
        assert "incumbent LEIDOS, INC." in header and "2027-02-28 pop_potential_end" in header
        assert "office HRSA HEADQUARTERS" in header

        await pilot.press("escape")
        await pilot.press("1")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert app.screen.query_one("#dates", DataTable).row_count == 0  # beyond 60 days
        assert "identify 1" in app.screen.query_one("#attention", DataTable).border_title


async def test_pursuit_screen_shows_the_assessment_and_accepts_tasks(app: MentorTop) -> None:
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.push_screen(PursuitScreen(1))
        await pilot.pause()
        panel = text(app, "#assessment")
        assert panel.startswith("fit 72 · go · deep qwen3:14b · profile v- · 900 in / 120 out")
        assert "3. [capture] Price to win" in panel
        before = app.screen.query_one("#tasks", DataTable).row_count

        await pilot.press("x")
        await pilot.pause()
        assert app.screen.query_one("#tasks", DataTable).row_count == before + 2  # one existed
        await pilot.press("x")
        await pilot.pause()
        assert app.screen.query_one("#tasks", DataTable).row_count == before + 2
