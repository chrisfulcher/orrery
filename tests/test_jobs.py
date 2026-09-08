from datetime import date
from pathlib import Path

import pytest

from mentor import jobs
from mentor.config import Settings
from mentor.embed.pipeline import EmbedResult
from mentor.extract.text import ExtractResult
from mentor.fetch.queue import FetchResult
from mentor.ingest import awards, bulk, entities, notices
from mentor.ingest.awards import AwardsResult
from mentor.ingest.bulk import BulkResult
from mentor.ingest.entities import EntitiesResult
from mentor.ingest.notices import IngestResult
from mentor.jobs import (
    JOBS,
    OPERATIONS,
    JobFailed,
    coerce,
    default_window,
    needs_unmet,
    summarize,
)
from mentor.progress import JobCancelled
from mentor.sam.client import SamError


def test_registry_lists_the_operations_with_their_needs() -> None:
    assert list(OPERATIONS) == [
        "ingest-notices", "ingest-bulk", "ingest-awards", "ingest-entities", "fetch",
        "extract", "embed", "summarize", "assess", "db-migrate", "db-reindex",
    ]  # fmt: skip
    assert [n for n in JOBS if n not in OPERATIONS] == [
        "probe-sam", "probe-embed", "probe-fast", "probe-deep"
    ]  # fmt: skip
    assert JOBS["probe-sam"].needs == {"sam_key", "naics"} and not JOBS["probe-sam"].cancellable
    assert JOBS["ingest-notices"].needs == {"sam_key", "naics"}
    assert JOBS["fetch"].needs == {"sam_key"} and JOBS["ingest-awards"].needs == {"naics"}
    assert not JOBS["assess"].cancellable and JOBS["fetch"].cancellable


def test_needs_unmet_uses_the_cli_words(tmp_path: Path) -> None:
    bare = Settings(_env_file=None, data_dir=tmp_path)
    assert needs_unmet(JOBS["ingest-notices"], bare, {}) == [
        "MENTOR_SAM_API_KEY is not set", "MENTOR_NAICS is empty; nothing to ingest",
    ]  # fmt: skip
    assert needs_unmet(JOBS["extract"], bare, {}) == []
    # entities need the key only when something must be downloaded or looked up
    assert needs_unmet(JOBS["ingest-entities"], bare, {}) == ["MENTOR_SAM_API_KEY is not set"]
    assert needs_unmet(JOBS["ingest-entities"], bare, {"file": "x.zip"}) == []
    (tmp_path / "extracts" / "sam").mkdir(parents=True)
    (tmp_path / "extracts" / "sam" / "SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.ZIP").write_bytes(b"")
    assert needs_unmet(JOBS["ingest-entities"], bare, {}) == []
    assert needs_unmet(JOBS["ingest-entities"], bare, {"refresh": "true"}) == [
        "MENTOR_SAM_API_KEY is not set"
    ]
    assert needs_unmet(JOBS["ingest-entities"], bare, {"uei": "A"}) == [
        "MENTOR_SAM_API_KEY is not set"
    ]


def test_coerce_parses_form_strings_and_reports_usage_errors() -> None:
    values = coerce(
        JOBS["ingest-awards"], {"since": "2024-10-01", "until": date(2025, 9, 30), "limit": "5"}
    )
    assert values == {
        "since": date(2024, 10, 1),
        "until": date(2025, 9, 30),
        "file": None,
        "limit": 5,
    }
    assert coerce(JOBS["ingest-entities"], {"uei": "a, b", "refresh": "yes"}) == {
        "uei": ["a", "b"], "file": None, "refresh": True, "limit": None,
    }  # fmt: skip
    assert coerce(JOBS["assess"], {"pursuit_id": "3"}) == {"pursuit_id": 3, "slot": "deep"}
    with pytest.raises(JobFailed, match="Pursuit id is required") as raised:
        coerce(JOBS["assess"], {})
    assert raised.value.exit_code == 2
    with pytest.raises(JobFailed, match="Model slot: must be one of deep, fast"):
        coerce(JOBS["assess"], {"pursuit_id": 1, "slot": "medium"})
    with pytest.raises(JobFailed, match="Attachment limit"):
        coerce(JOBS["extract"], {"limit": "many"})


def test_default_windows() -> None:
    assert default_window("ingest-notices", date(2026, 9, 7)) == (
        date(2026, 9, 6),
        date(2026, 9, 7),
    )
    assert default_window("ingest-awards", date(2026, 9, 7)) == (date(2023, 9, 7), date(2026, 9, 7))
    assert default_window("ingest-awards", date(2028, 2, 29)) == (
        date(2025, 2, 28),
        date(2028, 2, 29),
    )


def test_run_plumbs_parameters_and_callbacks(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    def fake_ingest(conn_, settings_, **kwargs):
        calls.append(kwargs)
        kwargs["report"]("page 1")
        return IngestResult(1, 5, 5, 5, 20, 1)

    monkeypatch.setattr(notices, "ingest_notices", fake_ingest)
    lines: list[str] = []
    result = jobs.run(
        JOBS["ingest-notices"], conn, settings, {"since": "2026-09-01"}, report=lines.append
    )
    assert result.notices_seen == 5 and lines == ["page 1"]
    assert calls[0]["posted_from"] == date(2026, 9, 1) and calls[0]["posted_to"] >= date(2026, 9, 1)

    with pytest.raises(JobFailed, match="--since must not be after --until") as raised:
        jobs.run(
            JOBS["ingest-notices"], conn, settings, {"since": "2026-09-09", "until": "2026-09-01"}
        )
    assert raised.value.exit_code == 2

    def fake_bulk(conn_, settings_, path, **kwargs):
        calls.append({"path": path, **kwargs})
        return BulkResult(2, 10, 3, 3, 0, 0, 3, 0, 0)

    monkeypatch.setattr(bulk, "ingest_bulk", fake_bulk)
    lines = []
    jobs.run(
        JOBS["ingest-bulk"], conn, settings, {"file": str(tmp_path / "x.csv")}, report=lines.append
    )
    assert calls[-1]["mark_inactive"] is False and calls[-1]["path"] == tmp_path / "x.csv"
    assert lines == [f"extract: {tmp_path / 'x.csv'}"]
    with pytest.raises(JobFailed, match="mutually exclusive"):
        jobs.run(JOBS["ingest-bulk"], conn, settings, {"file": "x", "archived": "2025"})


def test_run_maps_adapter_errors_to_job_failed(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise SamError("HTTP 500")

    monkeypatch.setattr(notices, "ingest_notices", boom)
    with pytest.raises(JobFailed, match="ingestion stopped: HTTP 500") as raised:
        jobs.run(JOBS["ingest-notices"], conn, settings, {})
    assert raised.value.exit_code == 1

    def stop(*args, **kwargs):
        raise JobCancelled

    monkeypatch.setattr(notices, "ingest_notices", stop)
    with pytest.raises(JobCancelled):
        jobs.run(JOBS["ingest-notices"], conn, settings, {})


def test_awards_wait_is_cancellable_through_sleep(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def fake_fetch(settings_, **kwargs):
        captured.update(kwargs)
        kwargs["sleep"](0)  # the wait loop calls this between polls
        raise AssertionError("not reached")

    monkeypatch.setattr(awards, "fetch_awards", fake_fetch)
    with pytest.raises(JobCancelled):
        jobs.run(JOBS["ingest-awards"], conn, settings, {}, cancelled=lambda: True)
    assert captured["since"] == default_window("ingest-awards")[0]


def test_entities_runner_chooses_lookup_or_extract(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        entities,
        "lookup_entities",
        lambda c, s, ueis, **kw: (
            seen.append(f"lookup {ueis}") or EntitiesResult(1, 1, 1, 0, 0, 1, 5, 1, 0)
        ),
    )
    monkeypatch.setattr(
        entities,
        "ingest_extract",
        lambda c, s, path, **kw: (
            seen.append(f"extract {path.name}") or EntitiesResult(2, 3, 2, 0, 0, 2, 9, 0, 0)
        ),
    )
    jobs.run(JOBS["ingest-entities"], conn, settings, {"uei": "A,B"})
    jobs.run(JOBS["ingest-entities"], conn, settings, {"file": str(tmp_path / "e.zip")})
    assert seen == ["lookup ['A', 'B']", "extract e.zip"]
    with pytest.raises(JobFailed, match="mutually exclusive"):
        jobs.run(JOBS["ingest-entities"], conn, settings, {"uei": "A", "file": "x"})


def test_db_jobs_run_for_real(conn, settings: Settings) -> None:
    assert jobs.run(JOBS["db-migrate"], conn, settings, {}) == []
    assert jobs.run(JOBS["db-reindex"], conn, settings, {}) is None
    assert summarize(JOBS["db-migrate"], []) == "up to date"
    assert summarize(JOBS["db-migrate"], ["0001_initial.sql"]) == "applied 0001_initial.sql"
    assert summarize(JOBS["db-reindex"], None) == "search index rebuilt"


def test_summaries_match_the_cli() -> None:
    assert summarize(JOBS["ingest-notices"], IngestResult(3, 5, 2, 1, 20, 1)) == (
        "run 3: 5 notices seen, 2 new, 1 versions, 20 attachments, 1 requests"
    )
    assert summarize(JOBS["ingest-bulk"], BulkResult(4, 100, 9, 9, 0, 3, 9, 2, 50)) == (
        "run 4: 100 rows read, 9 in slice, 9 new, 0 updated, 3 descriptions filled, 9 versions,"
        " 2 marked inactive (resumed at row 50)"
    )
    assert summarize(JOBS["ingest-awards"], AwardsResult(5, 10, 10, 10, 0, 4, 9, 0, 0)) == (
        "run 5: 10 rows read, 10 in slice, 10 new, 0 updated, 4 contractors new, 9 offices and"
        " 0 vendors unresolved"
    )
    assert summarize(JOBS["ingest-entities"], EntitiesResult(6, 3, 2, 0, 1, 2, 11, 0, 0)) == (
        "run 6: 3 registrants read, 2 in slice, 0 malformed, 1 contractors new, 2 registrations,"
        " 11 facts, 0 requests"
    )
    assert summarize(JOBS["fetch"], FetchResult(7, 2, 1, 3, 0, 1, 2, True)) == (
        "run 7: 2 descriptions fetched, 1 failed; 3 attachments fetched, 0 failed, 1 skipped;"
        " 2 requests (daily budget exhausted; attachments still fetched)"
    )
    assert (
        summarize(JOBS["extract"], ExtractResult(1, 2, 3)) == "1 extracted, 2 unsupported, 3 failed"
    )
    assert summarize(JOBS["embed"], EmbedResult(1, 2, 9, "m")) == (
        "1 notices, 2 attachments, 9 chunks embedded with m"
    )
