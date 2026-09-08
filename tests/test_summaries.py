import json
import sqlite3
from collections.abc import Callable

import pytest
from test_assess import FakeBackend

from mentor import summaries
from mentor.ai import AIError, Completion
from mentor.config import Settings
from mentor.progress import JobCancelled
from mentor.summaries import NoticeInputs, SummarizeResult, render, set_aside_fit

Seed = Callable[[dict | None], None]

GOOD = {
    "summary": "Buys  help desk staffing for a headquarters office.\nResponses are due 2026-09-20.",
    "work_type": "it",
    "keywords": ["Help Desk", "help desk", " staffing ", ""],
    "set_aside": "SBA",
}
CLEAN = {
    "summary": "Buys help desk staffing for a headquarters office. Responses are due 2026-09-20.",
    "work_type": "it",
    "keywords": ["help desk", "staffing"],
    "set_aside": "SBA",
}


@pytest.fixture
def described(conn: sqlite3.Connection, seed: Seed) -> list[str]:
    """Two notices with fetched descriptions, in id order."""
    seed()
    ids = [n for (n,) in conn.execute("SELECT notice_id FROM notices ORDER BY id LIMIT 2")]
    for i, notice_id in enumerate(ids):
        conn.execute(
            "UPDATE notices SET description = ?, description_status = 'fetched'"
            " WHERE notice_id = ?",
            (f"Description {i} of a help desk requirement.", notice_id),
        )
    return ids


def rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT notice_id, slot, provider, model, prompt_version, length(inputs_hash),"
        " input_tokens, output_tokens, raw_response, result, summary, work_type, keywords,"
        " stated_set_aside FROM notice_summaries ORDER BY summary_id"
    ).fetchall()


def test_render_is_bounded_and_carries_the_typed_fields() -> None:
    inputs = NoticeInputs(
        "N1", "Help desk", "Solicitation", "HHS.HRSA.HQ", "541512", "D302", None,
        "2026-09-01", "2026-09-20", "word " * 2_000,
    )  # fmt: skip
    text = render(inputs, context_chars=2_000)
    assert text.startswith(
        "# Help desk\nSolicitation · HHS › HRSA › HQ · NAICS 541512 · PSC D302 · set-aside -"
        " · posted 2026-09-01 · deadline 2026-09-20\n\n# Description\nword word"
    )
    assert text.endswith(" […]")
    assert len(summaries.SYSTEM_PROMPT) + len(text) <= 2_000
    assert render(inputs, context_chars=10).endswith("# Description\n […]")


def test_summarize_stores_the_result_with_provenance(
    conn: sqlite3.Connection, settings: Settings, described: list[str]
) -> None:
    backend = FakeBackend([json.dumps(GOOD)])
    lines: list[str] = []
    result = summaries.summarize_pending(conn, settings, backend=backend, report=lines.append)
    assert result == SummarizeResult(2, 0, "qwen3:14b")
    assert not backend.closed
    assert lines == [f"notice {n}: it · {CLEAN['summary'][:60]}" for n in described]
    assert rows(conn) == [
        (n, "fast", "openai", "qwen3:14b", 1, 64, 200, 50, json.dumps(GOOD),
         json.dumps(CLEAN, sort_keys=True), CLEAN["summary"], "it", '["help desk", "staffing"]',
         "SBA")
        for n in described
    ]  # fmt: skip
    system, user = backend.calls[0]
    assert system == summaries.SYSTEM_PROMPT
    assert "# Description\nDescription 0 of a help desk requirement." in user
    assert conn.execute("SELECT count(*) FROM v_notice_summaries").fetchone() == (2,)

    # a second run finds nothing to do and makes no call
    assert summaries.summarize_pending(conn, settings, backend=backend) == (
        SummarizeResult(0, 0, "qwen3:14b")
    )
    assert len(backend.calls) == 2

    # the deep slot is the fast one until configured, so it is done too
    assert summaries.summarize_pending(conn, settings, slot="deep", backend=backend) == (
        SummarizeResult(0, 0, "qwen3:14b")
    )
    assert len(backend.calls) == 2


def test_invalid_answer_is_recorded_and_the_walk_continues(
    conn: sqlite3.Connection, settings: Settings, described: list[str]
) -> None:
    backend = FakeBackend(["nope", "still nope", json.dumps(GOOD)])
    lines: list[str] = []
    result = summaries.summarize_pending(conn, settings, backend=backend, report=lines.append)
    assert result == SummarizeResult(1, 1, "qwen3:14b")
    assert lines[0].startswith(f"notice {described[0]}: invalid answer: fake-model did not")
    first, second = rows(conn)
    assert first[:5] == (described[0], "fast", "openai", "qwen3:14b", 1)
    assert first[6:] == (400, 100, "still nope", None, None, None, None, None)
    assert second[9] == json.dumps(CLEAN, sort_keys=True)
    assert conn.execute("SELECT notice_id FROM v_notice_summaries").fetchall() == [(described[1],)]
    # the failed notice is not retried under the same model and prompt version
    assert summaries.summarize_pending(conn, settings, backend=backend) == (
        SummarizeResult(0, 0, "qwen3:14b")
    )
    assert len(backend.calls) == 3


class RefusingBackend(FakeBackend):
    def complete(self, *, system: str, user: str, schema: dict, max_tokens: int) -> Completion:
        from mentor.ai import InvalidResponse

        self.calls.append((system, user))
        raise InvalidResponse("fake-model declined the request (cyber)")


def test_a_refusal_is_recorded_without_a_completion(
    conn: sqlite3.Connection, settings: Settings, described: list[str]
) -> None:
    result = summaries.summarize_pending(conn, settings, backend=RefusingBackend([""]), limit=1)
    assert result == SummarizeResult(0, 1, "qwen3:14b")
    [row] = rows(conn)
    assert row[0] == described[0]
    assert row[6:10] == (None, None, "fake-model declined the request (cyber)", None)


class FailingBackend(FakeBackend):
    def complete(self, *, system: str, user: str, schema: dict, max_tokens: int) -> Completion:
        if len(self.calls) == 1:
            raise AIError("http://fake: connection refused")
        return super().complete(system=system, user=user, schema=schema, max_tokens=max_tokens)


def test_a_transport_error_ends_the_run_and_keeps_earlier_rows(
    conn: sqlite3.Connection, settings: Settings, described: list[str]
) -> None:
    with pytest.raises(AIError, match="connection refused"):
        summaries.summarize_pending(conn, settings, backend=FailingBackend([json.dumps(GOOD)]))
    assert [r[0] for r in rows(conn)] == [described[0]]


def test_cancellation_between_notices_and_the_limit(
    conn: sqlite3.Connection, settings: Settings, described: list[str]
) -> None:
    backend = FakeBackend([json.dumps(GOOD)])
    lines: list[str] = []
    with pytest.raises(JobCancelled):
        summaries.summarize_pending(
            conn, settings, backend=backend, report=lines.append, cancelled=lambda: bool(lines)
        )
    assert [r[0] for r in rows(conn)] == [described[0]]
    assert summaries.summarize_pending(conn, settings, backend=backend, limit=0) == (
        SummarizeResult(0, 0, "qwen3:14b")
    )
    assert summaries.summarize_pending(conn, settings, backend=backend, limit=1) == (
        SummarizeResult(1, 0, "qwen3:14b")
    )
    assert [r[0] for r in rows(conn)] == described


def test_owned_backend_is_closed(
    conn: sqlite3.Connection, settings: Settings, described: list[str], monkeypatch
) -> None:
    backend = FakeBackend([json.dumps(GOOD)])
    monkeypatch.setattr("mentor.ai.backend_for", lambda slot: backend)
    assert summaries.summarize_pending(conn, settings, limit=1).summarized == 1
    assert backend.closed


@pytest.mark.parametrize(
    ("code", "stated", "size", "held", "expected"),
    [
        (None, None, "small", [], "unknown"),
        ("SBA", "8A", None, [], "unknown"),
        ("NONE", "SBA", "small", [], "open"),
        (None, "NONE", "small", [], "open"),
        ("SBA", None, "small", [], "eligible"),
        (None, "sba", "small", [], "eligible"),
        ("SBA", None, "other-than-small", [], "ineligible"),
        ("8A", None, "small", [], "ineligible"),
        ("8AN", None, "small", ["8A"], "eligible"),
        ("SDVOSBS", None, "small", ["sdvosbc"], "eligible"),
        ("WOSB", None, "small", ["EDWOSB"], "eligible"),
        ("EDWOSB", None, "small", ["WOSB"], "ineligible"),
        ("HZC", None, None, ["SBA"], "ineligible"),
        ("ISBEE", None, "small", [], "eligible"),
    ],
)
def test_set_aside_fit(code, stated, size, held, expected) -> None:
    assert set_aside_fit(code, stated, size=size, set_asides=held) == expected
