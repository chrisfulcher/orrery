import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

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
    assert result.output.strip() == "applied 0001_initial.sql"
    assert (tmp_path / "mentor.sqlite").exists()

    result = runner.invoke(app, ["db", "migrate"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "up to date"

    result = runner.invoke(app, ["db", "status"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"database: {tmp_path / 'mentor.sqlite'}",
        "applied  0001_initial.sql",
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
