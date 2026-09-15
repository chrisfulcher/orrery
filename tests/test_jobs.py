from datetime import date
from pathlib import Path

import pytest

from orrery import jobs, summaries
from orrery.config import Settings
from orrery.embed.pipeline import EmbedResult
from orrery.extract.text import ExtractResult
from orrery.fetch import queue
from orrery.fetch.queue import FetchResult
from orrery.ingest import awards, bulk, entities, exclusions, notices
from orrery.ingest.awards import AwardsResult
from orrery.ingest.bulk import BulkResult
from orrery.ingest.entities import EntitiesResult
from orrery.ingest.exclusions import ExclusionsResult
from orrery.ingest.notices import IngestResult
from orrery.jobs import (
    JOBS,
    MANIFESTS_PER_RUN,
    OPERATIONS,
    JobFailed,
    coerce,
    default_window,
    needs_unmet,
    summarize,
)
from orrery.progress import JobCancelled
from orrery.sam.client import SamError
from orrery.summaries import SummarizeResult


def test_registry_lists_the_operations_with_their_needs() -> None:
    assert list(OPERATIONS) == [
        "ingest-notices", "ingest-bulk", "ingest-awards", "ingest-entities",
        "ingest-exclusions", "fetch", "extract", "embed", "summarize", "sync", "assess",
        "db-migrate", "db-reindex",
    ]  # fmt: skip
    assert [n for n in JOBS if n not in OPERATIONS] == [
        "probe-sam", "probe-embed", "probe-fast", "probe-deep"
    ]  # fmt: skip
    assert JOBS["probe-sam"].needs == {"sam_key", "naics"} and not JOBS["probe-sam"].cancellable
    assert JOBS["ingest-notices"].needs == {"sam_key", "naics"}
    # fetch needs no key: only its description stage is keyed, and that stage stands aside.
    assert JOBS["fetch"].needs == set() and JOBS["ingest-awards"].needs == {"naics"}
    # Exclusions are a public file matched against contractors: no key, and no slice either.
    assert JOBS["ingest-exclusions"].needs == set()
    assert not JOBS["assess"].cancellable and JOBS["fetch"].cancellable


def test_needs_unmet_uses_the_cli_words(tmp_path: Path) -> None:
    bare = Settings(_env_file=None, data_dir=tmp_path)
    assert needs_unmet(JOBS["ingest-notices"], bare, {}) == [
        "ORRERY_SAM_API_KEY is not set", "ORRERY_NAICS is empty; nothing to ingest",
    ]  # fmt: skip
    assert needs_unmet(JOBS["extract"], bare, {}) == []
    # entities need the key only when something must be downloaded or looked up
    assert needs_unmet(JOBS["ingest-entities"], bare, {}) == ["ORRERY_SAM_API_KEY is not set"]
    assert needs_unmet(JOBS["ingest-entities"], bare, {"file": "x.zip"}) == []
    (tmp_path / "extracts" / "sam").mkdir(parents=True)
    (tmp_path / "extracts" / "sam" / "SAM_PUBLIC_UTF-8_MONTHLY_V2_20260906.ZIP").write_bytes(b"")
    assert needs_unmet(JOBS["ingest-entities"], bare, {}) == []
    assert needs_unmet(JOBS["ingest-entities"], bare, {"refresh": "true"}) == [
        "ORRERY_SAM_API_KEY is not set"
    ]
    assert needs_unmet(JOBS["ingest-entities"], bare, {"uei": "A"}) == [
        "ORRERY_SAM_API_KEY is not set"
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


def test_a_cleared_field_still_takes_the_declared_default() -> None:
    """The CLI passes every parameter, so a None or "" would otherwise outrank the default:
    fetch's manifest cap became LIMIT -1 and swept the whole backlog."""
    assert coerce(JOBS["fetch"], {"budget": None, "max_attachments": None})["max_manifests"] == (
        MANIFESTS_PER_RUN
    )
    assert coerce(JOBS["fetch"], {"max_manifests": ""})["max_manifests"] == MANIFESTS_PER_RUN
    assert coerce(JOBS["fetch"], {"max_manifests": 5})["max_manifests"] == 5
    assert coerce(JOBS["summarize"], {"slot": ""})["slot"] == "fast"
    assert coerce(JOBS["extract"], {"limit": None})["limit"] is None


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


def _stub_sync_stages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides) -> list[str]:
    """Stand in for all five stages, recording the order they ran in."""
    order: list[str] = []
    results = {
        "ingest_bulk": BulkResult(1, 10, 3, 3, 0, 0, 3, 0, 0, bulk.ACTIVE_PASS_DONE),
        "exclusions": ExclusionsResult(6, 9, 1, 4, 1, 8, 0, exclusions.TERMINATION_DONE),
        "fetch": FetchResult(2, 4, 0, 4, 0, 9, 9, 0, 0, 4, False),
        "extract": ExtractResult(9, 2, 0, {"ole": 2}),
        "summarize": SummarizeResult(3, 0, "qwen3:14b"),
        "embed": EmbedResult(3, 9, 40, "nomic-embed-text"),
    } | overrides

    def stage(name):
        def run(*args, **kwargs):
            order.append(name)
            return results[name]

        return run

    monkeypatch.setattr(
        bulk,
        "fetch_extract",
        lambda *a, **k: bulk.Extract(tmp_path / "x.csv", "2026-09-08T00:00:00Z"),
    )
    monkeypatch.setattr(bulk, "ingest_bulk", stage("ingest_bulk"))
    monkeypatch.setattr(
        exclusions,
        "fetch_extract",
        lambda *a, **k: bulk.Extract(tmp_path / "exclusions.ZIP", None),
    )
    monkeypatch.setattr(exclusions, "ingest_extract", stage("exclusions"))
    monkeypatch.setattr(queue, "fetch_pending", stage("fetch"))
    monkeypatch.setattr(jobs, "extract_pending", stage("extract"))
    monkeypatch.setattr(summaries, "summarize_pending", stage("summarize"))
    monkeypatch.setattr(jobs, "embed_pending", stage("embed"))
    return order


def test_sync_runs_every_stage_in_order(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order = _stub_sync_stages(monkeypatch, tmp_path)
    lines: list[str] = []

    result = jobs.run(JOBS["sync"], conn, settings, {}, report=lines.append)

    assert order == ["ingest_bulk", "fetch", "extract", "summarize", "embed"]
    assert result.skipped == ()
    assert result.extract.done == 9 and result.embed.chunks == 40
    # The roll-up is each stage's own summary, so the unsupported kinds reach it unchanged.
    roll_up = summarize(JOBS["sync"], result)
    assert "extract: 9 extracted, 2 unsupported (ole 2), 0 failed" in roll_up
    # Each stage announces itself, so a long run says where it is.
    assert [line for line in lines if line.startswith("sync: ")] == [
        "sync: ingest bulk", "sync: ingest exclusions",
        "sync: no contractor in the store yet, so an exclusion could match nothing",
        "sync: fetch", "sync: extract", "sync: summarize", "sync: embed",
    ]  # fmt: skip
    assert result.exclusions is None


def test_sync_reads_exclusions_once_the_store_has_a_contractor(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The file is 12 MB of vendors this store may know nothing about; with no contractor in
    it there is nothing to match, and the download is the one cost a keyless source still has."""
    order = _stub_sync_stages(monkeypatch, tmp_path)
    conn.execute(
        "INSERT INTO entities (kind, name, uei, source_id, first_seen_at, last_seen_at)"
        " VALUES ('contractor', 'EXAMPLE LOGISTICS LLC', 'EXCL00000001', 'usaspending_awards',"
        " '2026-09-09T00:00:00Z', '2026-09-09T00:00:00Z')"
    )

    result = jobs.run(JOBS["sync"], conn, settings, {}, report=lambda _: None)

    assert order == ["ingest_bulk", "exclusions", "fetch", "extract", "summarize", "embed"]
    assert result.exclusions.rows_matched == 1
    assert "ingest-exclusions: run 6:" in summarize(JOBS["sync"], result)


def test_sync_without_ai_skips_exactly_the_model_stages(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order = _stub_sync_stages(monkeypatch, tmp_path)

    result = jobs.run(JOBS["sync"], conn, settings, {"no_ai": "yes"}, report=lambda _: None)

    assert order == ["ingest_bulk", "fetch", "extract"]
    assert result.skipped == ("summarize", "embed")
    assert result.summarize is None and result.embed is None


def test_sync_reports_a_spent_budget_and_keeps_going(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The budget is not a failure: the stages after fetch spend no quota and still run."""
    spent = FetchResult(2, 0, 0, 0, 0, 0, 0, 0, 0, 10, True)
    order = _stub_sync_stages(monkeypatch, tmp_path, fetch=spent)
    lines: list[str] = []

    result = jobs.run(JOBS["sync"], conn, settings, {}, report=lines.append)

    assert order == ["ingest_bulk", "fetch", "extract", "summarize", "embed"]
    assert result.fetch.budget_exhausted is True
    assert "sync: today's SAM.gov budget is spent; descriptions resume tomorrow" in lines


def test_sync_stops_at_the_first_failing_stage(
    conn, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order = _stub_sync_stages(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise SamError("HTTP 500")

    monkeypatch.setattr(queue, "fetch_pending", boom)
    with pytest.raises(JobFailed, match="sync stopped: HTTP 500"):
        jobs.run(JOBS["sync"], conn, settings, {})
    assert order == ["ingest_bulk"]


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
    done = BulkResult(4, 100, 9, 9, 0, 3, 9, 2, 50, bulk.ACTIVE_PASS_DONE)
    assert summarize(JOBS["ingest-bulk"], done) == (
        "run 4: 100 rows read, 9 in slice, 9 new, 0 updated, 3 descriptions filled, 9 versions,"
        " 2 marked inactive (resumed at row 50)"
    )
    # A pass that did not run never reports a count: 0 deactivated would read as "nothing to
    # deactivate", which is the claim the pass is not entitled to make.
    skipped = BulkResult(4, 100, 9, 9, 0, 3, 9, 0, 0, bulk.ACTIVE_PASS_NO_CUT)
    assert summarize(JOBS["ingest-bulk"], skipped).endswith(
        f"9 versions, active pass {bulk.ACTIVE_PASS_NO_CUT}"
    )
    assert summarize(JOBS["ingest-awards"], AwardsResult(5, 10, 10, 10, 0, 4, 9, 0, 0)) == (
        "run 5: 10 rows read, 10 in slice, 10 new, 0 updated, 4 contractors new, 9 offices and"
        " 0 vendors unresolved"
    )
    assert summarize(JOBS["ingest-entities"], EntitiesResult(6, 3, 2, 0, 1, 2, 11, 0, 0)) == (
        "run 6: 3 registrants read, 2 in slice, 0 malformed, 1 contractors new, 2 registrations,"
        " 11 facts, 0 requests"
    )
    ended = ExclusionsResult(8, 168452, 3, 133266, 1, 9, 2, exclusions.TERMINATION_DONE)
    assert summarize(JOBS["ingest-exclusions"], ended) == (
        "run 8: 168452 rows read, 133266 individuals not read, 3 matched a contractor, 1 new,"
        " 9 facts, 2 ended"
    )
    # A capped run never reports a count of endings: it cannot tell one the file dropped from
    # one it never reached, and 0 ended would be the claim that it can.
    capped = ExclusionsResult(9, 1, 1, 0, 1, 9, 0, exclusions.TERMINATION_PARTIAL)
    assert summarize(JOBS["ingest-exclusions"], capped).endswith(
        f"termination pass {exclusions.TERMINATION_PARTIAL}"
    )
    assert summarize(JOBS["fetch"], FetchResult(7, 2, 1, 9, 4, 12, 3, 0, 1, 2, True)) == (
        "run 7: 2 descriptions fetched, 1 failed; 9 manifests read, 4 failed,"
        " 12 attachments found; 3 attachments fetched, 0 failed, 1 skipped;"
        " 2 requests (daily budget exhausted; attachments still fetched)"
    )
    assert (
        summarize(JOBS["extract"], ExtractResult(1, 2, 3)) == "1 extracted, 2 unsupported, 3 failed"
    )
    # Which readers a store is missing, so "unsupported" is an answer rather than a count.
    assert summarize(JOBS["extract"], ExtractResult(1, 3, 0, {"pptx": 2, "ole": 1})) == (
        "1 extracted, 3 unsupported (pptx 2, ole 1), 0 failed"
    )
    assert summarize(JOBS["embed"], EmbedResult(1, 2, 9, "m")) == (
        "1 notices, 2 attachments, 9 chunks embedded with m"
    )
