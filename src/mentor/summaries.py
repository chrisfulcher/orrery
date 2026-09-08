"""Notice summaries and tags on the fast slot (DESIGN.md §4, AI tiering; §8 notice_summaries).

``mentor summarize`` walks the notices that have a fetched description and no row for the
slot's model and prompt version, renders each into one prompt bounded by the slot's character
budget (the description takes what the head line leaves), asks for a two-sentence summary, a
work type from a fixed list, keywords, and the set-aside the text itself states, and stores
one row per notice with provenance. An answer that fails validation (or a refusal) is stored
as a row with a null result so it is not retried under the same model and prompt version;
any other model error ends the run, and the rows written so far stand. No profile data goes
into the prompt: the set-aside fit against the company profile is ``set_aside_fit``, derived
at read time from the notice's code, the stated one, and the profile's qualifications.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mentor import ai, db
from mentor.assess import clip
from mentor.config import Settings
from mentor.progress import Cancelled, Report, check, never, quiet

PROMPT_VERSION = 1

SYSTEM_PROMPT = """\
You write for a small-business capture team scanning U.S. federal contract notices. From
the notice given, write summary: two short sentences, the first saying what is being bought
and for whom, the second how and when to respond (notice type, set-aside, deadline) or, when
the notice is not a solicitation, what it announces. Classify work_type from the list. Give
keywords: three to eight lowercase tags a vendor would search for (technologies, services,
products, standards), never agency names. Set set_aside to the SAM.gov code of the set-aside
the notice text itself states, or null when the text does not say; the code in the header may
be missing while the text is explicit. Answer with one JSON object matching the schema and
nothing else.
"""

WORK_TYPES = (
    "services",
    "it",
    "construction",
    "supplies",
    "research",
    "professional",
    "medical",
    "other",
)
WorkType = Literal[
    "services", "it", "construction", "supplies", "research", "professional", "medical", "other"
]

SET_ASIDE_CODES = (
    "NONE", "SBA", "SBP", "8A", "8AN", "HZC", "HZS", "SDVOSBC", "SDVOSBS",
    "WOSB", "WOSBSS", "EDWOSB", "EDWOSBSS", "VSA", "VSS", "ISBEE",
)  # fmt: skip
SetAsideCode = Literal[
    "NONE", "SBA", "SBP", "8A", "8AN", "HZC", "HZS", "SDVOSBC", "SDVOSBS",
    "WOSB", "WOSBSS", "EDWOSB", "EDWOSBSS", "VSA", "VSS", "ISBEE",
]  # fmt: skip

# Sole-source variants and the program they belong to.
BASE_PROGRAM = {
    "8AN": "8A",
    "HZS": "HZC",
    "SDVOSBS": "SDVOSBC",
    "WOSBSS": "WOSB",
    "EDWOSBSS": "EDWOSB",
    "VSS": "VSA",
}
SMALL_BUSINESS_CODES = frozenset({"SBA", "SBP", "ISBEE"})

Fit = Literal["eligible", "ineligible", "open", "unknown"]
KEYWORDS = 8
MAX_TOKENS = 4_096  # thinking tokens count against it on both providers


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        description="Two short sentences: what is bought and for whom; how and when to respond."
    )
    work_type: WorkType = Field(
        description=(
            "services: facilities, logistics, maintenance, operations. it: software, systems,"
            " networks, cybersecurity, telecom. construction: build, renovate, architecture and"
            " engineering. supplies: products, equipment, parts. research: R&D, studies,"
            " prototypes. professional: consulting, staffing, training, program support."
            " medical: clinical and health services, medical equipment. other: none of these."
        )
    )
    keywords: list[str] = Field(
        description="Three to eight lowercase search tags: technologies, services, products."
    )
    set_aside: SetAsideCode | None = Field(
        description=(
            "The set-aside the notice text states, as SAM.gov spells it: NONE (full and open),"
            " SBA (total small business), SBP (partial small business), 8A, 8AN (8(a) sole"
            " source), HZC (HUBZone), HZS (HUBZone sole source), SDVOSBC (service-disabled"
            " veteran-owned), SDVOSBS (its sole source), WOSB, WOSBSS, EDWOSB, EDWOSBSS,"
            " VSA (veteran-owned), VSS, ISBEE (emerging small business). Null when unstated."
        )
    )

    @field_validator("summary")
    @classmethod
    def _one_paragraph(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("summary is empty")
        return value

    @field_validator("keywords")
    @classmethod
    def _tidy_keywords(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for keyword in value:
            keyword = " ".join(keyword.split()).lower()
            if keyword and keyword not in seen:
                seen.append(keyword)
        if not seen:
            raise ValueError("keywords is empty")
        return seen[:KEYWORDS]


@dataclass(frozen=True)
class NoticeInputs:
    notice_id: str
    title: str
    notice_type: str | None
    agency_path_name: str | None
    naics_code: str | None
    psc_code: str | None
    set_aside_code: str | None
    posted_at: str | None
    response_deadline: str | None
    description: str


@dataclass(frozen=True)
class SummarizeResult:
    summarized: int
    failed: int
    model: str


PENDING = """
SELECT n.notice_id, n.title, n.notice_type, n.full_parent_path_name, n.naics_code, n.psc_code,
       n.set_aside_code, n.posted_at, n.response_deadline, n.description
FROM notices AS n
WHERE n.description_status = 'fetched' AND n.description <> ''
  AND NOT EXISTS (SELECT 1 FROM notice_summaries AS s WHERE s.notice_id = n.notice_id
                  AND s.model = :model AND s.prompt_version = :version)
ORDER BY n.id LIMIT :limit
"""

INSERT = """
INSERT INTO notice_summaries (notice_id, slot, provider, model, prompt_version, inputs_hash,
    input_tokens, output_tokens, raw_response, result, summary, work_type, keywords,
    stated_set_aside, created_at)
VALUES (:notice_id, :slot, :provider, :model, :prompt_version, :inputs_hash, :input_tokens,
    :output_tokens, :raw_response, :result, :summary, :work_type, :keywords,
    :stated_set_aside, :created_at)
"""


def head(inputs: NoticeInputs) -> str:
    """The fixed first line of the prompt: the notice's typed fields."""
    agency = (inputs.agency_path_name or "-").replace(".", " › ")
    return (
        f"# {inputs.title}\n{inputs.notice_type or '-'} · {agency}"
        f" · NAICS {inputs.naics_code or '-'} · PSC {inputs.psc_code or '-'}"
        f" · set-aside {inputs.set_aside_code or '-'} · posted {inputs.posted_at or '-'}"
        f" · deadline {inputs.response_deadline or '-'}"
    )


def render(inputs: NoticeInputs, *, context_chars: int) -> str:
    """The user message, deterministic for the same inputs, within ``context_chars``."""
    top = head(inputs)
    budget = max(context_chars - len(SYSTEM_PROMPT) - len(top) - 200, 0)
    return f"{top}\n\n# Description\n{clip(inputs.description, budget)}"


def _sum(values: Iterable[int | None]) -> int | None:
    values = list(values)
    return None if not values or None in values else sum(values)


def summarize_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    slot: ai.SlotName = "fast",
    limit: int | None = None,
    backend: ai.ChatBackend | None = None,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> SummarizeResult:
    """Summarize every pending notice on the slot; ``limit`` caps notices this run."""
    chosen = ai.resolve_slot(settings, slot)
    rows = conn.execute(
        PENDING,
        {"model": chosen.model, "version": PROMPT_VERSION, "limit": -1 if limit is None else limit},
    ).fetchall()
    summarized = failed = 0
    own = backend is None
    backend = backend or ai.backend_for(chosen)
    try:
        for row in rows:
            check(cancelled)
            inputs = NoticeInputs(*row)
            user = render(inputs, context_chars=chosen.context_chars)
            parsed: Summary | None = None
            try:
                parsed, completions = ai.complete_structured(
                    backend,
                    system=SYSTEM_PROMPT,
                    user=user,
                    model_type=Summary,
                    max_tokens=MAX_TOKENS,
                )
            except ai.InvalidResponse as exc:
                completions = exc.completions
                raw = completions[-1].text if completions else str(exc)
                failed += 1
                report(f"notice {inputs.notice_id}: invalid answer: {exc}")
            else:
                raw = completions[-1].text
                summarized += 1
                report(f"notice {inputs.notice_id}: {parsed.work_type} · {parsed.summary[:60]}")
            conn.execute(
                INSERT,
                {
                    "notice_id": inputs.notice_id,
                    "slot": chosen.name,
                    "provider": chosen.provider,
                    "model": chosen.model,
                    "prompt_version": PROMPT_VERSION,
                    "inputs_hash": hashlib.sha256((SYSTEM_PROMPT + user).encode()).hexdigest(),
                    "input_tokens": _sum(c.input_tokens for c in completions),
                    "output_tokens": _sum(c.output_tokens for c in completions),
                    "raw_response": raw,
                    "result": None
                    if parsed is None
                    else json.dumps(parsed.model_dump(), sort_keys=True),
                    "summary": None if parsed is None else parsed.summary,
                    "work_type": None if parsed is None else parsed.work_type,
                    "keywords": None if parsed is None else json.dumps(parsed.keywords),
                    "stated_set_aside": None if parsed is None else parsed.set_aside,
                    "created_at": db.utcnow(),
                },
            )
    finally:
        if own:
            backend.close()
    return SummarizeResult(summarized, failed, chosen.model)


def set_aside_fit(
    code: str | None,
    stated: str | None,
    *,
    size: str | None,
    set_asides: Iterable[str],
) -> Fit:
    """Whether a company with these qualifications can bid: the notice's own code wins, the
    stated one fills in when it is empty. ``unknown`` when neither says, or the profile has
    no size and no set-aside codes."""
    held = {s.upper() for s in set_asides}
    if size is None and not held:
        return "unknown"
    effective = (code or stated or "").upper()
    if not effective:
        return "unknown"
    if effective == "NONE":
        return "open"
    program = BASE_PROGRAM.get(effective, effective)
    if program in held:
        return "eligible"
    if program in SMALL_BUSINESS_CODES and size == "small":
        return "eligible"
    if program == "WOSB" and "EDWOSB" in held:
        return "eligible"
    return "ineligible"


__all__ = [
    "Fit",
    "NoticeInputs",
    "PROMPT_VERSION",
    "SET_ASIDE_CODES",
    "SYSTEM_PROMPT",
    "Summary",
    "SummarizeResult",
    "WORK_TYPES",
    "render",
    "set_aside_fit",
    "summarize_pending",
]
