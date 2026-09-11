"""The AI assessment of a pursuit against the company profile (DESIGN.md §4, AI tiering).

Inputs are gathered through the query and workspace modules, rendered into one prompt in a
fixed order, sent to the deep slot (or the fast one), validated against ``Assessment``, and
stored append-only with provenance. The prompt is bounded by the slot's character budget:
fixed parts are trimmed individually (the capability statement to 4,000 characters, AI notes
to 2,000, each notice description to 3,000, ten office awards, eight items per registration
fact list, five related notices), and attachment text takes what remains, capped at half the
budget, from the three extracted attachments of the linked notices that best match the
pursuit's title and summary; each contributes the head of its text with page breaks marked.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orrery import ai, documents, query, workspace
from orrery.config import Settings
from orrery.documents import ProfileDocument

PROMPT_VERSION = 2

SYSTEM_PROMPT = """\
You are a capture analyst for a U.S. federal government contractor. You judge whether the
company described in the profile should pursue the requirement described, using only the
material provided: the profile, the pursuit, the notices and document excerpts, the
incumbent's registration and award record, and the office's award history. Be specific and
cite the material (a notice title, a PIID, a NAICS code, a set-aside) in every reason. Say
what is missing under open_questions rather than guessing. Recommend the gate decision the
material supports: go, no-go, or hold. Score fit from 0 (no fit) to 100 (ideal) and make the
score agree with the decision. Suggested tasks are concrete next actions for the pursuit's
current or next stage, using the stage keys given. Answer with one JSON object matching the
schema and nothing else.
"""

STATEMENT_CHARS = 4_000
AI_NOTES_CHARS = 2_000
DESCRIPTION_CHARS = 3_000
AWARDS = 10
RELATED = 5
EXCERPTS = 3
EXCERPT_SHARE = 0.5


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SuggestedTask(_Model):
    title: str
    stage: str


class Assessment(_Model):
    fit: int = Field(
        description="How well the company fits this requirement, 0 (no fit) to 100 (ideal)."
    )
    fit_reasons: list[str] = Field(
        description="Specific reasons for the fit score, citing the material."
    )
    gaps: list[str] = Field(description="What the company lacks for this requirement.")
    incumbent_standing: str = Field(description="How strong the incumbent's position is, and why.")
    competitive_picture: str = Field(
        description="Who else is likely to compete, from the award history."
    )
    decision: Literal["go", "no-go", "hold"] = Field(
        description="The gate decision the material supports."
    )
    decision_why: str = Field(description="The rationale for the decision.")
    open_questions: list[str] = Field(description="What is unknown or missing from the material.")
    suggested_tasks: list[SuggestedTask] = Field(
        description="Concrete next actions for the current or next stage."
    )

    @field_validator("fit")
    @classmethod
    def _percent(cls, value: int) -> int:
        return max(0, min(100, value))


@dataclass(frozen=True)
class Excerpt:
    filename: str
    notice_title: str
    text: str


@dataclass(frozen=True)
class Inputs:
    profile_version: int | None
    profile: ProfileDocument | None
    detail: workspace.PursuitDetail
    notices: tuple[query.NoticeDetail, ...]
    excerpts: tuple[Excerpt, ...]
    """Full extracted text, best match first; ``render`` trims to the budget."""
    incumbent_name: str | None
    incumbent_facts: tuple[tuple[str, str], ...]
    office_awards: tuple[query.ContractRef, ...]
    stage_keys: tuple[str, ...]


def gather(conn, pursuit_id: int, *, user_id: int = workspace.USER_ID) -> Inputs:
    """Everything the prompt may use, read through the query and workspace modules."""
    detail = workspace.pursuit(conn, pursuit_id, user_id=user_id)
    p = detail.pursuit
    latest = workspace.latest_document(conn, "profile", user_id=user_id)
    profile = None
    if latest is not None:
        try:
            profile = documents.parse(latest.body, ProfileDocument)
        except documents.DocumentError:
            profile = None
    notices = tuple(
        n for n in (query.notice(conn, link.notice_id) for link in detail.notices) if n is not None
    )
    titles = {n.notice_id: n.title for n in notices}
    ranked = query.rank_attachments(
        conn, tuple(n.notice_id for n in notices), f"{p.title} {p.summary or ''}"
    )
    excerpts = []
    for info, notice_id in ranked[:EXCERPTS]:
        text = query.attachment_text(conn, info.attachment_id)
        if text:
            excerpts.append(Excerpt(info.filename or info.url, titles.get(notice_id, ""), text))
    incumbent_name = detail.incumbent.vendor if detail.incumbent else None
    facts: list[tuple[str, str]] = []
    if detail.incumbent and detail.incumbent.vendor_uei:
        vendor = query.contractor(conn, detail.incumbent.vendor_uei)
        if vendor is not None:
            facts = query.summarize_facts(vendor.facts, max_items=8)
    awards = (
        query.awards(conn, office_code=p.office_code, naics=p.naics_code, limit=AWARDS)
        if p.office_code
        else []
    )
    return Inputs(
        latest.version if latest else None,
        profile,
        detail,
        notices,
        tuple(excerpts),
        incumbent_name,
        tuple(facts),
        tuple(awards),
        tuple(workspace.workflow(conn, user_id=user_id).keys()),
    )


def _join(values: list[str]) -> str:
    return ", ".join(values) or "-"


def clip(text: str | None, limit: int) -> str:
    """The head of ``text`` within ``limit`` characters, cut at a word and marked."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " […]"


def _pages(text: str) -> str:
    parts = text.split("\f")
    if len(parts) == 1:
        return text
    return "\n".join(
        f"[page {i}]\n{part.strip()}" for i, part in enumerate(parts, 1) if part.strip()
    )


def render(inputs: Inputs, *, context_chars: int) -> str:
    """The user message, deterministic for the same inputs, within ``context_chars``."""
    p = inputs.detail.pursuit
    out: list[str] = []
    if inputs.profile is None:
        out.append(
            "# Company profile\n"
            "No profile document has been saved; judge fit only from the pursuit."
        )
    else:
        doc = inputs.profile
        out.append(
            f"# Company profile (version {inputs.profile_version})\n"
            f"name: {doc.company.name or '-'} · UEI {doc.company.uei or '-'}"
            f" · CAGE {doc.company.cage or '-'}\n"
            f"NAICS (primary first): {_join(doc.offerings.naics)}\n"
            f"PSC: {_join(doc.offerings.psc)}\n"
            f"keywords: {_join(doc.offerings.keywords)}\n"
            f"size: {doc.qualifications.size or '-'}"
            f" · set-asides: {_join(doc.qualifications.set_asides)}"
            f" · certifications: {_join(doc.qualifications.certifications)}\n"
            f"target agencies: {_join(doc.markets.agency_prefixes)}"
            f" · offices: {_join(doc.markets.office_codes)}"
            f" · places: {_join(doc.markets.places)}\n"
            f"competitors: {_join([c.name or c.uei or '?' for c in doc.competitors])}\n"
            f"partners: {_join([c.name or c.uei or '?' for c in doc.partners])}\n"
            "capability statement: "
            f"{clip(doc.offerings.capability_statement, STATEMENT_CHARS) or '-'}\n"
            f"notes for AI: {clip(doc.ai.notes, AI_NOTES_CHARS) or '-'}"
        )
    tasks = inputs.detail.tasks
    open_tasks = [t.title for t in tasks if t.done_at is None]
    done_tasks = [t.title for t in tasks if t.done_at is not None]
    out.append(
        f"# Pursuit #{p.pursuit_id}: {p.title}\n"
        f"stage: {p.stage} · office: {p.office or '-'} ({p.office_code or '-'})"
        f" · NAICS {p.naics_code or '-'} · PWin {p.pwin if p.pwin is not None else '-'}"
        f"{' · held until ' + p.held_until if p.held_until else ''}\n"
        f"summary: {clip(p.summary, DESCRIPTION_CHARS) or '-'}\n"
        f"notes: {clip(p.notes, DESCRIPTION_CHARS) or '-'}\n"
        f"open tasks: {'; '.join(open_tasks) or '-'}\n"
        f"done tasks: {'; '.join(done_tasks) or '-'}"
    )
    if inputs.notices:
        lines = ["# Linked notices"]
        for n in inputs.notices:
            lines.append(
                f"- {n.title} ({n.notice_type or '-'}; {n.notice_id}) · posted {n.posted_at or '-'}"
                f" · deadline {n.response_deadline or '-'} · set-aside {n.set_aside_code or '-'}"
                f" · NAICS {n.naics_code or '-'} · {'active' if n.active else 'inactive'}\n"
                f"  {clip(n.description, DESCRIPTION_CHARS) or '(no description fetched)'}"
            )
        out.append("\n".join(lines))
    else:
        out.append("# Linked notices\nNone yet.")
    if inputs.incumbent_name or inputs.detail.incumbent:
        i = inputs.detail.incumbent
        lines = ["# Incumbent"]
        if i:
            lines.append(
                f"{i.vendor or '-'} · {i.piid} · {query_money(i.value_usd)} current"
                f" ({query_money(i.potential_value_usd)} potential) · {i.award_date or '-'} to"
                f" {i.pop_potential_end or i.pop_end or '-'} · set-aside {i.set_aside_code or '-'}"
                f" · competed {i.extent_competed_code or '-'}"
                f" · solicitation {i.solicitation_identifier or '-'}"
            )
        for predicate, value in inputs.incumbent_facts:
            lines.append(f"- {predicate}: {value}")
        related = inputs.detail.related_notices[:RELATED]
        if related:
            lines.append("prior solicitation notices: " + "; ".join(h.title for h in related))
        out.append("\n".join(lines))
    if inputs.office_awards:
        lines = ["# Recent awards at this office in this NAICS"]
        for a in inputs.office_awards[:AWARDS]:
            lines.append(
                f"- {a.last_action_date or '-'} {a.piid} {query_money(a.value_usd)}"
                f" set-aside {a.set_aside_code or '-'} to {a.vendor or '-'}"
            )
        out.append("\n".join(lines))
    out.append(
        "# Workflow stage keys\n"
        + ", ".join(inputs.stage_keys)
        + f"\nThe pursuit is in {p.stage!r}; suggested tasks belong to it or the next stage."
    )
    fixed = "\n\n".join(out)
    budget = min(int(context_chars * EXCERPT_SHARE), context_chars - len(fixed) - 200)
    if inputs.excerpts and budget > 400:
        share = budget // min(len(inputs.excerpts), EXCERPTS)
        lines = ["# Document excerpts (the best-matching attachments of the linked notices)"]
        for excerpt in inputs.excerpts[:EXCERPTS]:
            body = clip(_pages(excerpt.text), share)
            lines.append(f"## {excerpt.filename} ({excerpt.notice_title})\n{body}")
        fixed = fixed.replace(
            "# Workflow stage keys", "\n".join(lines) + "\n\n# Workflow stage keys", 1
        )
    elif not inputs.excerpts:
        fixed = fixed.replace(
            "# Workflow stage keys",
            "# Document excerpts\nNo extracted documents are linked; say so under"
            " open_questions.\n\n# Workflow stage keys",
            1,
        )
    return fixed


def query_money(value: float | None) -> str:
    return f"${value:,.0f}" if value is not None else "-"


def assess(
    conn,
    settings: Settings,
    pursuit_id: int,
    *,
    slot: ai.SlotName = "deep",
    backend: ai.ChatBackend | None = None,
    warn: Callable[[str], None] = lambda message: None,
    user_id: int = workspace.USER_ID,
) -> workspace.AssessmentRecord:
    """Run one assessment on the slot and store it. Nothing is written if the model fails."""
    chosen = ai.resolve_slot(settings, slot)
    inputs = gather(conn, pursuit_id, user_id=user_id)
    user = render(inputs, context_chars=chosen.context_chars)
    if len(SYSTEM_PROMPT) + len(user) > chosen.context_chars * 0.9:
        warn(
            f"prompt is {len(SYSTEM_PROMPT) + len(user):,} characters, near the {chosen.name}"
            f" slot's budget of {chosen.context_chars:,}; a local model may truncate it"
        )
    own = backend is None
    backend = backend or ai.backend_for(chosen)
    try:
        parsed, completions = ai.complete_structured(
            backend,
            system=SYSTEM_PROMPT,
            user=user,
            model_type=Assessment,
            max_tokens=16_000 if chosen.provider == "anthropic" else 4_096,
        )
    finally:
        if own:
            backend.close()
    current = inputs.detail.pursuit.stage
    parsed = parsed.model_copy(
        update={
            "suggested_tasks": [
                t if t.stage in inputs.stage_keys else t.model_copy(update={"stage": current})
                for t in parsed.suggested_tasks
            ]
        }
    )
    tokens_in = [c.input_tokens for c in completions]
    tokens_out = [c.output_tokens for c in completions]
    return workspace.save_assessment(
        conn,
        pursuit_id,
        slot=chosen.name,
        provider=chosen.provider,
        model=chosen.model,
        prompt_version=PROMPT_VERSION,
        profile_version=inputs.profile_version,
        inputs_hash=hashlib.sha256((SYSTEM_PROMPT + user).encode()).hexdigest(),
        input_tokens=None if None in tokens_in else sum(tokens_in),
        output_tokens=None if None in tokens_out else sum(tokens_out),
        raw_response=completions[-1].text,
        result=parsed.model_dump(),
        user_id=user_id,
    )


def accept_tasks(
    conn,
    pursuit_id: int,
    *,
    indices: list[int] | None = None,
    user_id: int = workspace.USER_ID,
) -> list[workspace.Task]:
    """Add the latest assessment's suggested tasks (all, or the 1-based ``indices``) that the
    pursuit does not already have, by title."""
    record = workspace.latest_assessment(conn, pursuit_id, user_id=user_id)
    if record is None:
        raise workspace.NotFound(f"pursuit {pursuit_id} has no assessment")
    detail = workspace.pursuit(conn, pursuit_id, user_id=user_id)
    existing = {t.title.strip().lower() for t in detail.tasks}
    suggested = [SuggestedTask.model_validate(t) for t in record.result.get("suggested_tasks", [])]
    if indices:
        for index in indices:
            if not 1 <= index <= len(suggested):
                raise ValueError(f"suggested task {index} does not exist (1 to {len(suggested)})")
        suggested = [suggested[i - 1] for i in indices]
    added = []
    for task in suggested:
        if task.title.strip().lower() in existing:
            continue
        stage = (
            task.stage if task.stage in workspace.workflow(conn, user_id=user_id).keys() else None
        )
        added.append(
            workspace.add_task(
                conn,
                task.title,
                subject_type="pursuit",
                subject_id=pursuit_id,
                stage=stage,
                user_id=user_id,
            )
        )
        existing.add(task.title.strip().lower())
    return added


def describe(record: workspace.AssessmentRecord) -> list[str]:
    """The stored assessment as lines of text, for the CLI and the TUI."""
    r = record.result
    tokens = (
        f"{record.input_tokens:,} in / {record.output_tokens:,} out"
        if record.input_tokens is not None and record.output_tokens is not None
        else "tokens unknown"
    )
    lines = [
        f"fit {r.get('fit', '?')} · {r.get('decision', '?')} · {record.slot} {record.model}"
        f" · profile v{record.profile_version or '-'} · {tokens} · {record.created_at}"
    ]
    for label, key in (
        ("why fit", "fit_reasons"),
        ("gaps", "gaps"),
        ("open questions", "open_questions"),
    ):
        items = r.get(key) or []
        if items:
            lines.append(f"{label}:")
            lines += [f"  - {item}" for item in items]
    for label, key in (
        ("incumbent", "incumbent_standing"),
        ("competition", "competitive_picture"),
        ("decision", "decision_why"),
    ):
        if r.get(key):
            lines.append(f"{label}: {r[key]}")
    tasks = r.get("suggested_tasks") or []
    if tasks:
        lines.append("suggested tasks (pursuit accept N):")
        lines += [
            f"  {i}. [{t.get('stage', '-')}] {t.get('title', '')}" for i, t in enumerate(tasks, 1)
        ]
    return lines


__all__ = [
    "Assessment",
    "Inputs",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "accept_tasks",
    "assess",
    "clip",
    "describe",
    "gather",
    "render",
]
