from pathlib import Path

from typer.testing import CliRunner

from mentor import __version__
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
