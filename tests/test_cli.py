import json
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import EMBED_URL, make_extract, register_fake_embeddings
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from mentor import __version__, db, runs
from mentor.cli import app
from mentor.ingest.bulk import ACTIVE_NAME, EXTRACT_URL, archive_name

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
        "applied 0003_extraction_and_search.sql",
        "applied 0004_embeddings.sql",
        "applied 0005_workspace.sql",
        "applied 0006_views.sql",
        "applied 0007_awards.sql",
        "applied 0008_registrations.sql",
        "applied 0009_documents.sql",
        "applied 0010_pursuits.sql",
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
        "applied  0003_extraction_and_search.sql",
        "applied  0004_embeddings.sql",
        "applied  0005_workspace.sql",
        "applied  0006_views.sql",
        "applied  0007_awards.sql",
        "applied  0008_registrations.sql",
        "applied  0009_documents.sql",
        "applied  0010_pursuits.sql",
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


FETCH_ENV = {
    "MENTOR_SAM_API_KEY": "test-key",
    "MENTOR_NAICS": "541512",
    "MENTOR_SAM_DAILY_BUDGET": "10",
    "MENTOR_FETCH_DELAY": "0",
}
SEARCH = re.compile(r".*/opportunities/v2/search.*")


def seed_via_cli(tmp_path: Path, httpx_mock: HTTPXMock) -> dict[str, str]:
    env = {**FETCH_ENV, "MENTOR_DATA_DIR": str(tmp_path)}
    assert runner.invoke(app, ["db", "migrate"], env=env).exit_code == 0
    fixture = json.loads((Path(__file__).with_name("fixtures") / "sam_search_v2.json").read_text())
    httpx_mock.add_response(url=SEARCH, json=fixture)
    assert runner.invoke(app, ["ingest", "notices"], env=env).exit_code == 0
    return env


def test_fetch_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.chdir(tmp_path)
    env = seed_via_cli(tmp_path, httpx_mock)

    result = runner.invoke(app, ["fetch", "--dry-run"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.startswith(
        "pending: 5 descriptions, 20 attachments; 9 requests remaining today"
    )
    assert len(result.output.splitlines()) == 6

    result = runner.invoke(app, ["fetch", "--dry-run", "--json"], env=env)
    payload = json.loads(result.output)
    assert set(payload) == {
        "descriptions_pending",
        "attachments_pending",
        "next_descriptions",
        "budget_remaining",
    }
    assert len(httpx_mock.get_requests()) == 1  # only the seed's search
    with closing(db.connect(tmp_path / "mentor.sqlite")) as conn:
        assert conn.execute("SELECT count(*) FROM ingestion_runs").fetchone() == (1,)


def test_fetch_json_and_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.chdir(tmp_path)
    env = seed_via_cli(tmp_path, httpx_mock)
    desc = json.loads((Path(__file__).with_name("fixtures") / "sam_noticedesc_v1.json").read_text())
    httpx_mock.add_response(url=re.compile(r".*noticedesc.*"), json=desc, is_reusable=True)
    httpx_mock.add_response(
        url=re.compile(r".*resources/files/.*"),
        content=b"%PDF-1.4",
        headers={"Content-Disposition": "attachment; filename=a.pdf"},
    )

    result = runner.invoke(app, ["fetch", "--json", "--max-attachments", "1"], env=env)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {
        "run_id",
        "descriptions_fetched",
        "descriptions_failed",
        "attachments_fetched",
        "attachments_failed",
        "attachments_skipped",
        "requests_spent",
        "budget_exhausted",
    }
    assert payload["descriptions_fetched"] == 5 and payload["attachments_fetched"] == 1

    result = runner.invoke(app, ["fetch"], env={**env, "MENTOR_SAM_API_KEY": ""})
    assert result.exit_code == 2 and "MENTOR_SAM_API_KEY" in result.output


def test_extract_search_and_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, make_pdf
) -> None:
    monkeypatch.chdir(tmp_path)
    env = seed_via_cli(tmp_path, httpx_mock)
    httpx_mock.add_response(
        url=re.compile(r".*resources/files/.*"),
        content=make_pdf(["Deliverables include a xylophone."]),
        headers={"Content-Disposition": "attachment; filename=sow.pdf"},
    )
    assert (
        runner.invoke(app, ["fetch", "--budget", "0", "--max-attachments", "1"], env=env).exit_code
        == 0
    )

    result = runner.invoke(app, ["extract"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "1 extracted, 0 unsupported, 0 failed"
    result = runner.invoke(app, ["extract", "--json"], env=env)
    assert json.loads(result.output) == {"done": 0, "unsupported": 0, "failed": 0}

    result = runner.invoke(app, ["search", "xylophone"], env=env)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 3 and lines[2].startswith("  sow.pdf: ") and "[xylophone]" in lines[2]

    result = runner.invoke(app, ["search", "xylophone", "--json"], env=env)
    payload = json.loads(result.output)
    assert len(payload) == 1 and set(payload[0]) == {
        "notice_id",
        "title",
        "agency",
        "response_deadline",
        "posted_at",
        "source",
        "snippet",
        "rank",
        "page",
    }

    assert runner.invoke(app, ["search", "nothing-here-zz"], env=env).output.strip() == "no matches"
    result = runner.invoke(app, ["search", ""], env=env)
    assert result.exit_code == 1 and "invalid query" in result.output

    result = runner.invoke(app, ["db", "reindex"], env=env)
    assert result.exit_code == 0 and result.output.strip() == "search index rebuilt"
    assert (
        len(json.loads(runner.invoke(app, ["search", "xylophone", "--json"], env=env).output)) == 1
    )


def test_embed_command_and_unreachable_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.chdir(tmp_path)
    env = seed_via_cli(tmp_path, httpx_mock)
    desc = json.loads((Path(__file__).with_name("fixtures") / "sam_noticedesc_v1.json").read_text())
    httpx_mock.add_response(url=re.compile(r".*noticedesc.*"), json=desc, is_reusable=True)
    assert runner.invoke(app, ["fetch", "--max-attachments", "0"], env=env).exit_code == 0

    httpx_mock.add_exception(httpx.ConnectError("refused"), url=EMBED_URL)
    result = runner.invoke(app, ["embed"], env=env)
    assert result.exit_code == 1, result.output
    assert "embedding stopped" in result.output and "http://localhost:11434/v1" in result.output
    assert "Traceback" not in result.output

    register_fake_embeddings(httpx_mock, [])
    result = runner.invoke(app, ["embed"], env=env)
    assert result.exit_code == 0, result.output
    assert (
        result.output.strip() == "5 notices, 0 attachments, 5 chunks embedded with nomic-embed-text"
    )
    result = runner.invoke(app, ["embed", "--json"], env=env)
    assert json.loads(result.output) == {
        "notices": 0,
        "attachments": 0,
        "chunks": 0,
        "model": "nomic-embed-text",
    }


def test_semantic_search_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, make_pdf
) -> None:
    monkeypatch.chdir(tmp_path)
    env = seed_via_cli(tmp_path, httpx_mock)
    httpx_mock.add_response(
        url=re.compile(r".*resources/files/.*"),
        content=make_pdf(["Deliverables include a xylophone."]),
        headers={"Content-Disposition": "attachment; filename=sow.pdf"},
    )
    assert (
        runner.invoke(app, ["fetch", "--budget", "0", "--max-attachments", "1"], env=env).exit_code
        == 0
    )
    assert runner.invoke(app, ["extract"], env=env).exit_code == 0
    register_fake_embeddings(httpx_mock, [])
    assert runner.invoke(app, ["embed"], env=env).exit_code == 0

    result = runner.invoke(app, ["search", "--semantic", "xylophone"], env=env)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[2].startswith("  sow.pdf p.1: Deliverables include a xylophone.")

    result = runner.invoke(app, ["search", "--semantic", "xylophone", "--json"], env=env)
    payload = json.loads(result.output)
    assert payload[0]["page"] == 1 and payload[0]["source"] == "sow.pdf"


def test_ingest_bulk_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.chdir(tmp_path)
    env = {**FETCH_ENV, "MENTOR_DATA_DIR": str(tmp_path)}
    assert runner.invoke(app, ["db", "migrate"], env=env).exit_code == 0
    local = tmp_path / "local.csv"
    local.write_bytes(make_extract([{}, {"NaicsCode": "236220"}]))

    result = runner.invoke(app, ["ingest", "bulk", "--file", str(local)], env=env)
    assert result.exit_code == 0, result.output
    assert "2 rows read, 1 in slice, 1 new" in result.output

    result = runner.invoke(app, ["ingest", "bulk", "--file", str(local), "--json"], env=env)
    payload = json.loads(
        result.output.split("\n", 1)[1] if result.output.startswith("extract:") else result.output
    )
    assert payload["rows_read"] == 0 and payload["resumed_from"] == 2

    httpx_mock.add_response(url=EXTRACT_URL.format(name=ACTIVE_NAME), content=make_extract([{}]))
    result = runner.invoke(app, ["ingest", "bulk"], env=env)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "extracts" / "ContractOpportunitiesFullCSV.csv").exists()

    assert runner.invoke(app, ["ingest", "bulk"], env={**env, "MENTOR_NAICS": ""}).exit_code == 2
    httpx_mock.add_response(url=EXTRACT_URL.format(name=archive_name(2025)), status_code=500)
    result = runner.invoke(app, ["ingest", "bulk", "--archived", "2025"], env=env)
    assert result.exit_code == 1 and "bulk ingest stopped" in result.output


def test_workspace_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.chdir(tmp_path)
    env = seed_via_cli(tmp_path, httpx_mock)
    fixture = json.loads((Path(__file__).with_name("fixtures") / "sam_search_v2.json").read_text())
    hrsa = fixture["opportunitiesData"][0]["noticeId"]

    def run(*args: str) -> object:
        return runner.invoke(app, list(args), env=env)

    result = run(
        "searches",
        "add",
        "sba",
        "--naics",
        "541512",
        "--set-aside",
        "SBA",
        "--deadline-days",
        "3650",
    )
    assert result.exit_code == 0 and result.output.strip() == "saved sba"
    assert len(json.loads(run("searches", "run", "sba", "--json").output)) == 2
    assert run("searches", "list").output.strip() == (
        "sba  naics=541512 set_asides=SBA deadline_within_days=3650"
    )
    search_toml = tmp_path / "search.toml"
    search_toml.write_text(
        'query = "Microsoft"\nnaics = ["541512", "541511"]\ndeadline_within_days = 30\n'
    )
    assert run("searches", "edit", "sba", "--file", str(search_toml)).output.strip() == "saved sba"
    assert run("searches", "list").output.strip() == (
        'sba  query="Microsoft" naics=541512,541511 deadline_within_days=30'
    )
    assert run("searches", "edit", "nope", "--file", str(search_toml)).exit_code == 1
    assert run("searches", "rm", "sba").exit_code == 0
    assert run("searches", "rm", "sba").exit_code == 1
    assert run("searches", "run", "sba").exit_code == 1

    result = run("track", hrsa, "--stage", "pursuing", "--pwin", "40")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("qualify:")
    assert run("track", hrsa, "--pwin", "55").exit_code == 0
    assert run("track", "nope").exit_code == 1
    events = json.loads(run("history", hrsa, "--json").output)
    assert [(e["field"], e["new_value"]) for e in events] == [
        ("stage", "identify"),
        ("notice", hrsa),
        ("gate", "go"),
        ("stage", "qualify"),
        ("pwin", "40"),
        ("pwin", "55"),
    ]
    assert run("history", "nope").exit_code == 1
    assert run("pipeline").output.startswith("qualify (1)")
    assert len(json.loads(run("pipeline", "--json").output)) == 1

    assert run("profile", "show").output.startswith("# mentor company profile")
    toml = tmp_path / "profile.toml"
    toml.write_text(
        '[company]\nname = "Example LLC"\n[offerings]\nnaics = ["541512", "541511"]\n'
        '[qualifications]\ncertifications = ["SB"]\n'
    )
    assert run("profile", "edit", "--file", str(toml)).output.strip() == "saved profile version 1"
    profile = json.loads(run("profile", "show", "--json").output)
    assert profile["naics"] == ["541512", "541511"] and profile["certifications"] == ["SB"]
    assert profile["document"]["offerings"]["naics"] == ["541512", "541511"]
    assert run("profile", "history").output.startswith("v1  ")
    toml.write_text("[company]\nnope = 1\n")
    bad = runner.invoke(app, ["profile", "edit", "--file", str(toml)], env=env)
    assert bad.exit_code == 1 and "invalid profile: company.nope" in bad.output

    assert run("search", "xylophone", "--naics", "999999").output.strip() == "no matches"
    assert run("search", "x", "--semantic", "--naics", "1").exit_code == 2
    assert run("workflow", "show").output.startswith("# mentor workflow")
    assert json.loads(run("workflow", "show", "--json").output)["stages"][0]["key"] == "identify"
    workflow_toml = tmp_path / "workflow.toml"
    workflow_toml.write_text('[[stages]]\nkey = "find"\nname = "Find"\ngate = "Go"\n')
    refused = run("workflow", "edit", "--file", str(workflow_toml))
    assert refused.exit_code == 1 and "in use by open pursuits" in refused.output
    workflow_toml.write_text('[[stages]]\nkey = "qualify"\nname = "Qualify"\ngate = "Go"\n')
    assert run("workflow", "edit", "--file", str(workflow_toml)).output.strip() == (
        "saved workflow version 1: qualify"
    )
    assert run("workflow", "history").output.startswith("v1  ")
    workflow_toml.write_text("stages = []\n")
    assert run("workflow", "edit", "--file", str(workflow_toml)).exit_code == 1


def test_ingest_awards_from_file(tmp_path: Path) -> None:
    from conftest import make_awards_csv

    env = {"MENTOR_DATA_DIR": str(tmp_path), "MENTOR_NAICS": "541512"}
    runner.invoke(app, ["db", "migrate"], env=env)
    path = tmp_path / "awards.csv"
    path.write_bytes(make_awards_csv([{}, {"award_id_piid": "B", "recipient_uei": "PHZDZ8SJ5CM1"}]))

    result = runner.invoke(app, ["ingest", "awards", "--file", str(path)], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == (
        "run 1: 2 rows read, 2 in slice, 2 new, 0 updated, 2 contractors new,"
        " 2 offices and 0 vendors unresolved"
    )

    result = runner.invoke(app, ["ingest", "awards", "--file", str(path), "--json"], env=env)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.split("\n", 1)[1])
    assert (payload["rows_read"], payload["resumed_from"]) == (
        0,
        2,
    )  # same file: resumed at its end


def test_ingest_awards_refuses_a_backwards_window(tmp_path: Path) -> None:
    env = {"MENTOR_DATA_DIR": str(tmp_path), "MENTOR_NAICS": "541512"}
    result = runner.invoke(
        app, ["ingest", "awards", "--since", "2026-01-02", "--until", "2026-01-01"], env=env
    )
    assert result.exit_code == 2
    assert "--since must not be after --until" in result.output


def test_awards_and_contractor_commands(tmp_path: Path) -> None:
    from conftest import make_awards_csv

    env = {"MENTOR_DATA_DIR": str(tmp_path), "MENTOR_NAICS": "541512"}
    runner.invoke(app, ["db", "migrate"], env=env)
    path = tmp_path / "awards.csv"
    path.write_bytes(make_awards_csv([{}]))
    runner.invoke(app, ["ingest", "awards", "--file", str(path)], env=env)

    result = runner.invoke(app, ["awards", "--uei", "UE9QJD4KK1L6"], env=env)
    assert result.exit_code == 0, result.output
    assert "75R60225F00001" in result.output and "$125,000" in result.output
    assert "LEIDOS, INC.  @ HRSA HEADQUARTERS" in result.output

    result = runner.invoke(app, ["awards", "--office", "NOPE"], env=env)
    assert result.output.strip() == "no awards"

    result = runner.invoke(app, ["contractor", "UE9QJD4KK1L6"], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:3] == [
        "LEIDOS, INC.  uei UE9QJD4KK1L6  cage 5UTE1",
        "also seen as: -",
        "awards: 1, $125,000 current value",
    ]

    result = runner.invoke(app, ["contractor", "UE9QJD4KK1L6", "--json"], env=env)
    assert json.loads(result.output)["awards"][0]["piid"] == "75R60225F00001"

    result = runner.invoke(app, ["contractor", "NOPE"], env=env)
    assert result.exit_code == 1 and "no contractor NOPE" in result.output


def test_ingest_entities_from_file_and_key_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    from conftest import EXTRACT_SAMPLE

    monkeypatch.chdir(tmp_path)  # no .env here: the key must come from the environment
    monkeypatch.delenv("MENTOR_SAM_API_KEY", raising=False)
    env = {"MENTOR_DATA_DIR": str(tmp_path), "MENTOR_NAICS": "611310"}
    runner.invoke(app, ["db", "migrate"], env=env)

    result = runner.invoke(app, ["ingest", "entities", "--file", str(EXTRACT_SAMPLE)], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == (
        "run 1: 3 registrants read, 1 in slice, 0 malformed, 1 contractors new,"
        " 1 registrations, 11 facts, 0 requests"
    )

    result = runner.invoke(app, ["ingest", "entities", "--uei", "C39LJA3KD378"], env=env)
    assert result.exit_code == 2 and "MENTOR_SAM_API_KEY" in result.output

    result = runner.invoke(app, ["ingest", "entities"], env=env)  # nothing on disk, no key
    assert result.exit_code == 2


def test_profile_edit_reopens_the_editor_until_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mentor import cli

    env = {"MENTOR_DATA_DIR": str(tmp_path)}
    runner.invoke(app, ["db", "migrate"], env=env)
    shown: list[str] = []
    answers = iter(['[company]\nname = "X"\ntypo = 1\n', '[company]\nname = "Fixed LLC"\n'])
    monkeypatch.setattr(cli, "_edit", lambda text: (shown.append(text), next(answers))[1])

    result = runner.invoke(app, ["profile", "edit"], env=env)
    assert result.exit_code == 0, result.output
    assert len(shown) == 2 and shown[1].startswith("# error: company.typo")
    assert (
        json.loads(runner.invoke(app, ["profile", "show", "--json"], env=env).output)["name"]
        == "Fixed LLC"
    )

    monkeypatch.setattr(cli, "_edit", lambda text: None)  # closed without saving
    result = runner.invoke(app, ["profile", "edit"], env=env)
    assert result.exit_code == 1 and "aborted, nothing saved" in result.output
    assert runner.invoke(app, ["profile", "history"], env=env).output.count("\n") == 1


def test_pursuit_commands_walk_the_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.chdir(tmp_path)
    env = seed_via_cli(tmp_path, httpx_mock)
    fixture = json.loads((Path(__file__).with_name("fixtures") / "sam_search_v2.json").read_text())
    hrsa = fixture["opportunitiesData"][0]["noticeId"]

    def run(*args: str) -> object:
        return runner.invoke(app, list(args), env=env)

    assert run("pursuits").output.strip() == "no pursuits (open)"
    opened = run(
        "pursuit",
        "new",
        "Help desk recompete",
        "--office",
        "75R602",
        "--naics",
        "541512",
        "--summary",
        "HRSA help desk",
    )
    assert opened.exit_code == 0, opened.output
    assert opened.output.startswith("opened: #1  Help desk recompete  @ HRSA HEADQUARTERS")
    assert run("pursuit", "link", "1", hrsa).output.strip().startswith("linked")
    assert run("pursuit", "task", "1", "Call the COR", "--due", "2026-09-10").exit_code == 0
    shown = run("pursuit", "show", "1").output
    assert (
        "stage identify → Pursuit Gate" in shown
        and "[ ]    4  2026-09-10  identify    Call the COR" in shown
    )
    assert run("pursuit", "done", "4").output.strip() == "done 4"
    assert run("pursuit", "set", "1", "--pwin", "40", "--notes", "weak incumbent").exit_code == 0
    assert run(
        "pursuit", "gate", "1", "hold", "--why", "budget unclear", "--until", "2026-10-01"
    ).output.startswith("hold:")
    assert run("pursuit", "gate", "1", "go", "--why", "fits").output.endswith("[qualify]\n")
    assert run("pursuit", "back", "1", "identify", "--why", "recheck").output.endswith(
        "[identify]\n"
    )
    assert run("pursuit", "gate", "1", "no-go", "--why", "no budget").output.startswith("no-go:")
    assert run("pursuits").output.strip() == "no pursuits (open)"
    assert run("pursuits", "--all").output.startswith("identify (1)")
    assert run("pursuit", "reopen", "1", "--why", "budget found").exit_code == 0
    assert run("pursuit", "outcome", "1", "won", "--why", "awarded").output.endswith(
        "[post-award]\n"
    )
    assert run("pursuit", "close", "1").output.startswith("closed:")
    detail = json.loads(run("pursuit", "show", "1", "--json").output)
    assert detail["pursuit"]["outcome"] == "won" and detail["pursuit"]["closed_at"]
    assert [e["field"] for e in detail["events"]][:4] == ["stage", "notice", "task", "task"]
    assert run("pursuit", "gate", "1", "go", "--why", "x").exit_code == 1
    assert run("pursuit", "show", "99").exit_code == 1
    assert run("pursuit", "new", "From award", "--contract", "999").exit_code == 1
