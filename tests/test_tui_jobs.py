"""Jobs inside `orrery top`: the worker, the Jobs tab, the dashboard line, `s` on a pursuit."""

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from conftest import SEARCH_FIXTURE, SEARCH_URL, register_fake_chat
from pytest_httpx import HTTPXMock
from test_assess import GOOD
from test_tui_setup import Seed, text
from textual.widgets import Input, RichLog

from orrery import db, dotenv, jobs, workspace
from orrery.config import Settings, env_values
from orrery.tui.app import DashboardScreen, OrreryTop, PursuitScreen
from orrery.tui.setup import SetupScreen
from orrery.tui.widgets import WrapTable


@pytest.fixture
def app(conn: sqlite3.Connection, settings: Settings, seed: Seed, tmp_path: Path) -> OrreryTop:
    seed()
    hrsa = SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"]
    workspace.track(conn, hrsa, stage="pursuing")
    conn.close()
    env_path = tmp_path / ".env"
    dotenv.write(env_path, env_values(settings))
    return OrreryTop(settings, env_path=env_path)


async def wait_idle(pilot, app: OrreryTop, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while app.current is not None and app.current.finished_at is None:
        assert time.monotonic() < deadline, "the job never finished"
        await pilot.pause(0.05)
    await pilot.pause(0.1)


def install_fake_job(monkeypatch: pytest.MonkeyPatch, run) -> None:
    fake = jobs.Job("fake", "Fake job", frozenset(), (), True, run)
    monkeypatch.setitem(jobs.JOBS, "fake", fake)
    monkeypatch.setattr(jobs, "OPERATIONS", (*jobs.OPERATIONS, "fake"))


async def test_jobs_tab_runs_a_job_on_its_own_connection_and_logs_it(
    app: OrreryTop, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    def run(conn, settings, values, report, cancelled) -> object:
        seen["thread"] = threading.get_ident()
        seen["conn"] = conn
        report("one")
        report("two")
        return "three done"

    install_fake_job(monkeypatch, run)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        assert isinstance(app.screen, SetupScreen)
        table = app.screen.query_one("#operations", WrapTable)
        assert table.has_focus and "idle" in table.border_title
        assert "Apply schema migrations" in [cells[0] for cells, _ in table.rows_data]
        table.move_cursor(row=table.row_count - 1)  # the fake job, appended last
        await pilot.press("r")
        await wait_idle(pilot, app)

        assert seen["thread"] != threading.get_ident() and seen["conn"] is not app.conn
        run_ = app.history[-1]
        assert run_.job.name == "fake" and run_.result == "three done" and run_.error is None
        assert list(run_.lines) == ["one", "two", "three done"]
        log = app.screen.query_one("#job_log", RichLog)
        assert "Fake job · done" in log.border_title
        assert [str(line.text) for line in log.lines] == ["one", "two", "three done"]
        assert "done: three done" in [cells[3] for cells, _ in table.rows_data]

        await pilot.press("escape")
        await pilot.press("1")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        queues = text(app, "#queues")
        assert "idle" in queues and "last: Fake job · three done" in queues


async def test_cancel_stops_a_job_between_items(
    app: OrreryTop, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    go_on = threading.Event()
    items: list[int] = []

    def run(conn, settings, values, report, cancelled) -> object:
        for i in range(50):
            started.set()
            go_on.wait(5)
            if cancelled():
                raise jobs.JobCancelled()
            items.append(i)
            report(f"item {i}")
        return "finished"

    install_fake_job(monkeypatch, run)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        assert app.run_job("fake", {})
        assert not app.run_job("fake", {})  # one at a time
        while not started.is_set():
            await pilot.pause(0.02)
        await pilot.press("j")
        await pilot.pause()
        assert "running" in app.screen.query_one("#operations", WrapTable).border_title
        await pilot.press("c")
        go_on.set()
        await wait_idle(pilot, app)
        assert app.history[-1].error == "cancelled" and items == []
        assert list(app.history[-1].lines)[-2:] == [
            "cancelling after the current item",
            "cancelled",
        ]
        assert app.is_running


async def test_a_failing_job_reports_and_the_app_survives(
    app: OrreryTop, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(conn, settings, values, report, cancelled) -> object:
        raise jobs.JobFailed("ingestion stopped: quota")

    install_fake_job(monkeypatch, run)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        assert app.run_job("fake", {})
        await wait_idle(pilot, app)
        assert app.history[-1].error == "ingestion stopped: quota"
        assert "last: Fake job · ingestion stopped: quota" in text(app, "#queues")
        assert app.run_job("fake", {})  # a failed job does not block the next one
        await wait_idle(pilot, app)


async def test_the_real_db_jobs_run_from_the_tab(app: OrreryTop) -> None:
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        assert app.run_job("db-migrate", {})
        await wait_idle(pilot, app)
        assert app.history[-1].lines[-1] == "up to date"
        assert app.run_job("db-reindex", {})
        await wait_idle(pilot, app)
        assert app.history[-1].lines[-1] == "search index rebuilt"


async def test_s_on_the_pursuit_screen_assesses_and_reloads(
    app: OrreryTop, httpx_mock: HTTPXMock
) -> None:
    requests: list[dict] = []
    register_fake_chat(httpx_mock, [json.dumps(GOOD)], requests)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        app.push_screen(PursuitScreen(1))
        await pilot.pause()
        assert "none yet: s to assess" in text(app, "#assessment")
        await pilot.press("s")
        await pilot.pause()
        assert app.current is not None and app.current.job.name == "assess"
        await wait_idle(pilot, app)
        assert len(requests) == 1
        panel = text(app, "#assessment")
        assert "fit 72" in panel and "go" in panel and "Call the COR" in panel
        assert "fit 72" in app.history[-1].lines[-1]


async def test_missing_needs_open_connections_instead_of_running(
    conn: sqlite3.Connection, seed: Seed, tmp_path: Path
) -> None:
    seed()
    conn.close()
    bare = Settings(_env_file=None, data_dir=tmp_path, naics=["541512"])  # no key
    env_path = tmp_path / ".env"
    dotenv.write(env_path, env_values(bare))
    app = OrreryTop(bare, env_path=env_path)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        assert not app.run_job("ingest-notices", {})
        await pilot.pause()
        assert app.current is None and isinstance(app.screen, SetupScreen)
        assert app.screen.query_one("#field-sam-api-key", Input) is not None


async def test_ingest_notices_runs_through_the_worker(
    app: OrreryTop, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=SEARCH_URL, json=SEARCH_FIXTURE)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        await pilot.press("r")  # ingest-notices is the first operation: its parameter modal
        await pilot.pause()
        app.screen.query_one("#field-since", Input).value = "2026-09-05"
        app.screen.query_one("#field-until", Input).value = "2026-09-06"
        await pilot.press("ctrl+s")
        await wait_idle(pilot, app)
        run_ = app.history[-1]
        assert run_.error is None, run_.error
        assert run_.lines[0].startswith("541512: page 1, ")
        assert run_.lines[-1].startswith("run 2: 5 notices seen, 0 new")
        table = app.screen.query_one("#operations", WrapTable)
        assert "succeeded" in table.rows_data[0][0][2]  # last run from ingestion_runs
        with db.connect(app.settings.db_path) as fresh:
            runs = fresh.execute("SELECT status FROM ingestion_runs ORDER BY run_id").fetchall()
        assert [r[0] for r in runs][-1] == "succeeded"
