import json
import sqlite3
from collections.abc import Callable

import pytest
from conftest import MOST_LINKS_NOTICE, SEARCH_FIXTURE

from mentor import assess, db, query, workspace
from mentor.ai import AIError, Completion, Slot
from mentor.assess import Assessment, gather, render
from mentor.config import Settings
from mentor.ingest.awards import AwardsResult

SeedAwards = Callable[[list[dict] | None], AwardsResult]
HRSA = SEARCH_FIXTURE["opportunitiesData"][0]["noticeId"]

GOOD = {
    "fit": 72,
    "fit_reasons": ["NAICS 541512 matches the primary", "SBA set-aside fits size"],
    "gaps": ["No past performance at HRSA"],
    "incumbent_standing": "Leidos holds 75R60222F00009 through 2027-02-28",
    "competitive_picture": "Two vendors won here recently",
    "decision": "go",
    "decision_why": "fits and the incumbent's options run out",
    "open_questions": ["Is the recompete set aside?"],
    "suggested_tasks": [
        {"title": "Call the COR", "stage": "qualify"},
        {"title": "Confirm the set-aside", "stage": "nonsense-stage"},
    ],
}


class FakeBackend:
    def __init__(
        self, replies: list[str], *, tokens: tuple[int | None, int | None] = (200, 50)
    ) -> None:
        self.slot = Slot("deep", "openai", "http://fake", "fake-model", None, 5.0, 48_000)
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.tokens = tokens
        self.closed = False

    def complete(self, *, system: str, user: str, schema: dict, max_tokens: int) -> Completion:
        self.calls.append((system, user))
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return Completion(text, self.tokens[0], self.tokens[1], "stop")

    def close(self) -> None:
        self.closed = True


def set_text(conn: sqlite3.Connection, attachment_id: int, text: str) -> None:
    conn.execute(
        "UPDATE attachments SET extracted_text = ?, extract_status = 'done',"
        " fetch_status = 'fetched' WHERE attachment_id = ?",
        (text, attachment_id),
    )


@pytest.fixture
def pursuit_with_everything(conn: sqlite3.Connection, seed_awards: SeedAwards) -> int:
    seed_awards()
    workspace.save_profile(
        conn,
        '[company]\nname = "Example LLC"\n[offerings]\nnaics = ["541512"]\n'
        'capability_statement = "Help desks and zero trust for civilian agencies."\n'
        '[qualifications]\nsize = "small"\nset_asides = ["SBA"]\n',
    )
    (contract_id,) = conn.execute(
        "SELECT contract_id FROM contracts WHERE piid = '75R60222F00009'"
    ).fetchone()
    p = workspace.new_pursuit(
        conn, "Help desk recompete", summary="HRSA needs a help desk", contract_id=contract_id
    )
    workspace.link_notice(conn, p.pursuit_id, HRSA)
    workspace.link_notice(conn, p.pursuit_id, MOST_LINKS_NOTICE)
    ids = [
        a
        for (a,) in conn.execute(
            "SELECT attachment_id FROM attachments WHERE notice_id = ?"
            " ORDER BY attachment_id LIMIT 3",
            (MOST_LINKS_NOTICE,),
        )
    ]
    set_text(conn, ids[0], "Boilerplate clauses about invoicing.\fMore clauses.")
    set_text(
        conn, ids[1], "Statement of work: the help desk shall answer calls.\fPage two of the SOW."
    )
    return p.pursuit_id


def test_gather_and_render_cover_every_section(
    conn: sqlite3.Connection, pursuit_with_everything: int
) -> None:
    inputs = gather(conn, pursuit_with_everything)
    assert inputs.profile_version == 1 and inputs.profile is not None
    assert {n.notice_id for n in inputs.notices} == {HRSA, MOST_LINKS_NOTICE}
    assert [e.text[:9] for e in inputs.excerpts] == ["Statement", "Boilerpla"]
    assert inputs.incumbent_name == "LEIDOS, INC." and inputs.office_awards
    assert inputs.stage_keys[0] == "identify"

    text = render(inputs, context_chars=48_000)
    assert text.index("# Company profile (version 1)") < text.index("# Pursuit #")
    assert "Help desks and zero trust" in text and "set-asides: SBA" in text
    assert "# Linked notices" in text and SEARCH_FIXTURE["opportunitiesData"][0]["title"] in text
    assert "# Incumbent" in text and "75R60222F00009" in text
    assert "# Recent awards at this office" in text and "CDW GOVERNMENT LLC" in text
    assert "# Document excerpts" in text and text.index("Statement of work") < text.index(
        "Boilerplate"
    )
    assert "[page 2]\nPage two of the SOW." in text
    assert text.rstrip().endswith("suggested tasks belong to it or the next stage.")


def test_render_without_profile_or_documents_says_so(conn: sqlite3.Connection) -> None:
    p = workspace.new_pursuit(conn, "Bare")
    inputs = gather(conn, p.pursuit_id)
    assert inputs.profile is None and inputs.profile_version is None and inputs.excerpts == ()
    text = render(inputs, context_chars=48_000)
    assert "No profile document has been saved" in text
    assert "No extracted documents are linked" in text and "# Linked notices\nNone yet." in text


def test_excerpts_respect_the_budget(
    conn: sqlite3.Connection, pursuit_with_everything: int
) -> None:
    (aid,) = conn.execute(
        "SELECT attachment_id FROM attachments WHERE extract_status = 'done'"
        " ORDER BY attachment_id LIMIT 1"
    ).fetchone()
    set_text(conn, aid, "word " * 20_000)
    inputs = gather(conn, pursuit_with_everything)
    text = render(inputs, context_chars=12_000)
    assert len(text) <= 12_000
    assert "[…]" in text


def test_assess_stores_the_result_with_provenance(
    conn: sqlite3.Connection,
    settings: Settings,
    pursuit_with_everything: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "utcnow", lambda: "2026-09-07T21:00:00Z")
    backend = FakeBackend([json.dumps(GOOD)])
    warnings: list[str] = []
    record = assess.assess(
        conn, settings, pursuit_with_everything, backend=backend, warn=warnings.append
    )
    assert (record.slot, record.provider, record.model) == ("fast", "openai", "qwen3:14b")
    assert (record.prompt_version, record.profile_version) == (1, 1)
    assert (record.input_tokens, record.output_tokens) == (200, 50)
    assert record.result["fit"] == 72 and record.result["decision"] == "go"
    assert record.result["suggested_tasks"][1]["stage"] == "identify"  # unknown stage mapped
    assert len(record.inputs_hash) == 64 and warnings == [] and backend.closed is False
    assert (
        backend.calls[0][0] == assess.SYSTEM_PROMPT
        and "# Company profile (version 1)" in backend.calls[0][1]
    )
    assert workspace.pursuit(conn, pursuit_with_everything).assessment == record

    added = assess.accept_tasks(conn, pursuit_with_everything)
    assert [(t.title, t.stage, t.origin) for t in added] == [
        ("Call the COR", "qualify", "user"),
        ("Confirm the set-aside", "identify", "user"),
    ]
    assert assess.accept_tasks(conn, pursuit_with_everything) == []
    with pytest.raises(ValueError, match="does not exist"):
        assess.accept_tasks(conn, pursuit_with_everything, indices=[9])


def test_assess_retries_then_fails_without_writing(
    conn: sqlite3.Connection, settings: Settings, pursuit_with_everything: int
) -> None:
    backend = FakeBackend(["nope", '{"fit": 1}'])
    with pytest.raises(AIError, match="did not return valid JSON"):
        assess.assess(conn, settings, pursuit_with_everything, backend=backend)
    assert len(backend.calls) == 2 and "previous answer was invalid" in backend.calls[1][1]
    assert workspace.latest_assessment(conn, pursuit_with_everything) is None
    with pytest.raises(workspace.NotFound, match="no assessment"):
        assess.accept_tasks(conn, pursuit_with_everything)


def test_assess_warns_when_the_prompt_nears_the_budget(
    conn: sqlite3.Connection, settings: Settings, pursuit_with_everything: int
) -> None:
    small = settings.model_copy(update={"ai_fast_context_chars": 3_000})
    warnings: list[str] = []
    assess.assess(
        conn,
        small,
        pursuit_with_everything,
        backend=FakeBackend([json.dumps(GOOD)]),
        warn=warnings.append,
    )
    assert warnings and "near the fast slot" in warnings[0]


def test_fit_is_clamped_and_schema_is_strict() -> None:
    parsed = Assessment.model_validate({**GOOD, "fit": 140})
    assert parsed.fit == 100
    schema = Assessment.model_json_schema()
    assert schema["additionalProperties"] is False and "suggested_tasks" in schema["required"]
    assert assess.describe(
        workspace.AssessmentRecord(
            1, 1, "deep", "openai", "m", 1, None, "h", None, None, "now", GOOD
        )
    )[0].startswith("fit 72 · go · deep m · profile v- · tokens unknown")


def test_rank_attachments_prefers_bm25_matches(
    conn: sqlite3.Connection, seed_awards: SeedAwards
) -> None:
    seed_awards()
    ids = [
        a
        for (a,) in conn.execute(
            "SELECT attachment_id FROM attachments WHERE notice_id = ?"
            " ORDER BY attachment_id LIMIT 3",
            (MOST_LINKS_NOTICE,),
        )
    ]
    set_text(conn, ids[0], "invoicing clauses")
    set_text(conn, ids[1], "help desk statement of work")
    set_text(conn, ids[2], "help desk help desk help desk")
    ranked = query.rank_attachments(conn, (MOST_LINKS_NOTICE,), "help desk")
    assert [info.attachment_id for info, _ in ranked] == [ids[2], ids[1], ids[0]]
    assert all(notice_id == MOST_LINKS_NOTICE for _, notice_id in ranked)
    assert query.rank_attachments(conn, (), "help desk") == []
    assert [
        info.attachment_id for info, _ in query.rank_attachments(conn, (MOST_LINKS_NOTICE,), "")
    ] == ids
    assert query.attachment_text(conn, ids[0]) == "invoicing clauses"
