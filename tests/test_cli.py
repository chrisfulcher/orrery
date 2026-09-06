import json
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from mentor import __version__, db, runs
from mentor.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"mentor {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "version" in result.output
    assert "db" in result.output


def test_db_migrate_and_status(tmp_path: Path) -> None:
    env = {"MENTOR_DATA_DIR": str(tmp_path)}

    result = runner.invoke(app, ["db", "migrate"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "applied 0001_initial.sql",
        "applied 0002_description_queue.sql",
    ]
    assert (tmp_path / "mentor.sqlite").exists()

    result = runner.invoke(app, ["db", "migrate"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "up to date"

    result = runner.invoke(app, ["db", "status"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"database: {tmp_path / 'mentor.sqlite'}",
        "applied  0001_initial.sql",
        "applied  0002_description_queue.sql",
    ]


def test_quota_text_and_json(tmp_path: Path) -> None:
    env = {"MENTOR_DATA_DIR": str(tmp_path)}
    assert runner.invoke(app, ["db", "migrate"], env=env).exit_code == 0

    result = runner.invoke(app, ["quota"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "spent 0 of 10 today (UTC), 10 remaining"

    with closing(db.connect(tmp_path / "mentor.sqlite")) as conn:
        run_id = runs.start(conn)
        for _ in range(2):
            conn.execute(
                "INSERT INTO api_requests (run_id, endpoint) VALUES (?, 'https://api.sam.gov/x')",
                (run_id,),
            )

    result = runner.invoke(app, ["quota", "--json"], env=env)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["spent"] == 2 and payload["budget"] == 10 and payload["remaining"] == 8
    assert payload["date"] == datetime.now(UTC).date().isoformat()


def test_ingest_notices_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.chdir(tmp_path)  # away from the repo's own .env
    env = {
        "MENTOR_DATA_DIR": str(tmp_path),
        "MENTOR_SAM_API_KEY": "test-key",
        "MENTOR_NAICS": "541512",
        "MENTOR_SAM_DAILY_BUDGET": "10",
    }
    assert runner.invoke(app, ["db", "migrate"], env=env).exit_code == 0
    fixture = json.loads((Path(__file__).with_name("fixtures") / "sam_search_v2.json").read_text())
    httpx_mock.add_response(url=re.compile(r".*/opportunities/v2/search.*"), json=fixture)
    httpx_mock.add_response(url=re.compile(r".*/opportunities/v2/search.*"), json=fixture)

    result = runner.invoke(
        app, ["ingest", "notices", "--since", "2026-09-05", "--until", "2026-09-06"], env=env
    )
    assert result.exit_code == 0, result.output
    assert "5 notices seen, 5 new" in result.output

    result = runner.invoke(app, ["ingest", "notices", "--json"], env=env)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {
        "run_id",
        "notices_seen",
        "notices_new",
        "versions_added",
        "attachments_added",
        "requests_spent",
    }
    assert payload["notices_new"] == 0

    result = runner.invoke(app, ["ingest", "notices"], env={**env, "MENTOR_NAICS": ""})
    assert result.exit_code == 2 and "MENTOR_NAICS" in result.output

    result = runner.invoke(app, ["ingest", "notices"], env={**env, "MENTOR_SAM_API_KEY": ""})
    assert result.exit_code == 2 and "MENTOR_SAM_API_KEY" in result.output


def test_ingest_notices_budget_stop_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.chdir(tmp_path)
    env = {
        "MENTOR_DATA_DIR": str(tmp_path),
        "MENTOR_SAM_API_KEY": "test-key",
        "MENTOR_NAICS": "541512,541511",
        "MENTOR_SAM_DAILY_BUDGET": "1",
    }
    assert runner.invoke(app, ["db", "migrate"], env=env).exit_code == 0
    fixture = json.loads((Path(__file__).with_name("fixtures") / "sam_search_v2.json").read_text())
    httpx_mock.add_response(url=re.compile(r".*/opportunities/v2/search.*"), json=fixture)

    result = runner.invoke(app, ["ingest", "notices"], env=env)

    assert result.exit_code == 1
    assert "ingestion stopped" in result.output
    with closing(db.connect(tmp_path / "mentor.sqlite")) as conn:
        assert conn.execute("SELECT status FROM ingestion_runs").fetchone() == ("failed",)
