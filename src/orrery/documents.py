"""Workspace documents: the company profile and the workflow as the user writes them, in TOML.

Pure functions and models, no database. A document is parsed with the standard library and
validated by a pydantic model that forbids unknown keys, so a typo is an error the editor
shows rather than a silently ignored field. Rendering produces a commented template; once a
document has been saved its body is kept verbatim, comments and all, so rendering is only
needed the first time and for saved searches. Serves docs/DESIGN.md §8 (workspace layer).
"""

import json
import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class DocumentError(ValueError):
    """The text is not valid TOML or does not fit the document's model."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _codes(values: list[str], *, length: int, what: str) -> list[str]:
    cleaned = []
    for value in values:
        code = value.strip()
        if not code:
            continue
        if len(code) != length or not code.isalnum():
            raise ValueError(f"{what} {code!r} is not {length} characters")
        cleaned.append(code)
    return cleaned


class Company(_Model):
    name: str = ""
    uei: str | None = None
    cage: str | None = None

    @field_validator("uei")
    @classmethod
    def _uei(cls, value: str | None) -> str | None:
        return _codes([value], length=12, what="UEI")[0].upper() if value else None

    @field_validator("cage")
    @classmethod
    def _cage(cls, value: str | None) -> str | None:
        return _codes([value], length=5, what="CAGE")[0].upper() if value else None


class Offerings(_Model):
    naics: list[str] = []
    """The first code is the primary."""
    psc: list[str] = []
    keywords: list[str] = []
    capability_statement: str = ""

    @field_validator("naics")
    @classmethod
    def _naics(cls, value: list[str]) -> list[str]:
        codes = _codes(value, length=6, what="NAICS code")
        if any(not code.isdigit() for code in codes):
            raise ValueError("NAICS codes are six digits")
        return codes


class Markets(_Model):
    agency_prefixes: list[str] = []
    """SAM.gov agency path prefixes such as 075 or 075.7526."""
    office_codes: list[str] = []
    """Activity address codes such as 75R602."""
    places: list[str] = []


class Qualifications(_Model):
    size: Literal["small", "other-than-small"] | None = None
    set_asides: list[str] = []
    """Set-aside codes the company can bid under, as SAM.gov spells them (SBA, 8A, SDVOSBC...)."""
    certifications: list[str] = []

    @field_validator("size", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if value == "" else value


class Party(_Model):
    uei: str | None = None
    name: str = ""
    notes: str = ""

    @field_validator("uei")
    @classmethod
    def _uei(cls, value: str | None) -> str | None:
        return _codes([value], length=12, what="UEI")[0].upper() if value else None


class AI(_Model):
    notes: str = ""
    """Anything an assessment should know that the structured fields cannot carry."""


class ProfileDocument(_Model):
    company: Company = Company()
    offerings: Offerings = Offerings()
    markets: Markets = Markets()
    qualifications: Qualifications = Qualifications()
    competitors: list[Party] = []
    partners: list[Party] = []
    ai: AI = AI()


def parse[M: BaseModel](text: str, model: type[M]) -> M:
    """Parse and validate, folding every problem into one readable ``DocumentError``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DocumentError(f"not valid TOML: {exc}") from exc
    return validate(data, model)


def validate[M: BaseModel](data: object, model: type[M]) -> M:
    """Validate already-parsed data (a form's values) with the same readable errors."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"]) or "(document)"
            problems.append(f"{where}: {error['msg']}")
        raise DocumentError("; ".join(problems)) from exc


def _s(value: str | None) -> str:
    """A TOML basic string (JSON's escapes are a subset of TOML's)."""
    return json.dumps(value or "", ensure_ascii=False)


def _list(values: list[str]) -> str:
    return "[" + ", ".join(_s(v) for v in values) + "]"


def _text(value: str) -> str:
    """A multi-line basic string for prose."""
    escaped = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""\n{escaped}"""' if escaped else '""'


def _parties(table: str, parties: list[Party]) -> str:
    if not parties:
        return f'# [[{table}]]\n# uei = ""\n# name = ""\n# notes = ""\n'
    return "".join(
        f"[[{table}]]\nuei = {_s(p.uei)}\nname = {_s(p.name)}\nnotes = {_s(p.notes)}\n\n"
        for p in parties
    )


def render_profile(doc: ProfileDocument) -> str:
    """The profile as a commented TOML document."""
    return f"""# orrery company profile. Edit, save, and close; an invalid document is shown again
# with the problem at the top. Every save is a new version. Unknown keys are errors.

[company]
name = {_s(doc.company.name)}
uei = {_s(doc.company.uei)}        # 12 characters, from SAM.gov; links the profile to its entity
cage = {_s(doc.company.cage)}

[offerings]
# NAICS codes you sell under; the first is the primary. These scope ingestion and the radar.
naics = {_list(doc.offerings.naics)}
psc = {_list(doc.offerings.psc)}
keywords = {_list(doc.offerings.keywords)}
capability_statement = {_text(doc.offerings.capability_statement)}

[markets]
# Agency path prefixes (075, 075.7526) and office codes (75R602) you target; places you serve.
agency_prefixes = {_list(doc.markets.agency_prefixes)}
office_codes = {_list(doc.markets.office_codes)}
places = {_list(doc.markets.places)}

[qualifications]
# size: "small" or "other-than-small". set_asides as SAM.gov spells them: SBA, 8A, SDVOSBC, HZC.
size = {_s(doc.qualifications.size) if doc.qualifications.size else '""'}
set_asides = {_list(doc.qualifications.set_asides)}
certifications = {_list(doc.qualifications.certifications)}

# Competitors and teaming partners, one table each; the UEI links them to the graph.
{_parties("competitors", doc.competitors)}
{_parties("partners", doc.partners)}
[ai]
# Anything an assessment should know that the fields above cannot carry.
notes = {_text(doc.ai.notes)}
"""


# The workflow: the shop's stages, the gate each feeds, and the tasks a stage starts with.


class StageSpec(_Model):
    key: str
    """A short slug used in commands and stored on pursuits: letters, digits, hyphens."""
    name: str
    gate: str | None = None
    """The gate this stage's work feeds; None for a stage with no decision at its end."""
    tasks: list[str] = []
    """Seeded as open tasks when a pursuit enters the stage."""

    @field_validator("key")
    @classmethod
    def _slug(cls, value: str) -> str:
        key = value.strip().lower()
        if not key or not all(c.isalnum() or c == "-" for c in key):
            raise ValueError(f"stage key {value!r} must be letters, digits, and hyphens")
        return key


class WorkflowDocument(_Model):
    stages: list[StageSpec]

    @field_validator("stages")
    @classmethod
    def _at_least_one_unique(cls, value: list[StageSpec]) -> list[StageSpec]:
        if not value:
            raise ValueError("a workflow needs at least one stage")
        keys = [stage.key for stage in value]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(f"duplicate stage keys {duplicates}")
        return value

    def keys(self) -> list[str]:
        return [stage.key for stage in self.stages]

    def stage(self, key: str) -> StageSpec:
        for stage in self.stages:
            if stage.key == key:
                return stage
        raise KeyError(key)

    def next_key(self, key: str) -> str | None:
        keys = self.keys()
        index = keys.index(key)
        return keys[index + 1] if index + 1 < len(keys) else None

    def previous_keys(self, key: str) -> list[str]:
        keys = self.keys()
        return keys[: keys.index(key)]

    def gated_keys(self) -> list[str]:
        return [stage.key for stage in self.stages if stage.gate]

    def first_key(self) -> str:
        return self.stages[0].key

    def last_key(self) -> str:
        return self.stages[-1].key

    def tasks_for(self, key: str) -> list[str]:
        return list(self.stage(key).tasks)


DEFAULT_WORKFLOW = """\
# orrery workflow: the stages a pursuit moves through, the gate each stage's work feeds,
# and the tasks a stage starts with. Streamlined Shipley by default; edit freely. A stage's
# key is what commands use and what pursuits store, so a key in use by an open pursuit
# cannot be removed. Every save is a new version.

[[stages]]
key = "identify"
name = "Identify"
gate = "Pursuit Gate"
tasks = [
    "Confirm the requirement: office, need, NAICS, vehicle",
    "Find the incumbent and the last award",
    "Check fit against the profile",
]

[[stages]]
key = "qualify"
name = "Qualify"
gate = "Capture Gate"
tasks = [
    "Confirm budget and timing with the customer",
    "Map the buying office and the decision makers",
    "Assess competitors and the incumbent's standing",
    "Estimate PWin and the cost to pursue",
]

[[stages]]
key = "capture"
name = "Capture"
gate = "Bid Gate"
tasks = [
    "Customer contact plan and call reports",
    "Shape the requirement where possible",
    "Teaming and subcontracting decisions",
    "Solution outline and discriminators",
    "Price-to-win",
]

[[stages]]
key = "proposal"
name = "Proposal Development"
gate = "Bid Confirmation Gate"
tasks = [
    "Kickoff and compliance matrix",
    "Pink team",
    "Red team",
    "Gold team and pricing sign-off",
    "Submit",
]

[[stages]]
key = "submitted"
name = "Submitted"
tasks = [
    "Answer evaluation notices and clarifications",
    "Prepare for orals or discussions",
]

[[stages]]
key = "post-award"
name = "Post Award"
tasks = [
    "Debrief",
    "Lessons learned",
    "Transition or protest decision",
]
"""


def render_workflow(doc: WorkflowDocument) -> str:
    out = [
        "# orrery workflow: the stages a pursuit moves through, the gate each stage's work feeds,\n"
        "# and the tasks a stage starts with. A stage key in use by an open pursuit cannot be\n"
        "# removed. Every save is a new version.\n"
    ]
    for stage in doc.stages:
        out.append(f"\n[[stages]]\nkey = {_s(stage.key)}\nname = {_s(stage.name)}\n")
        if stage.gate:
            out.append(f"gate = {_s(stage.gate)}\n")
        tasks = "".join(f"    {_s(task)},\n" for task in stage.tasks)
        out.append(f"tasks = [\n{tasks}]\n" if tasks else "tasks = []\n")
    return "".join(out)


# A saved search, for editing.


class SearchDocument(_Model):
    query: str | None = None
    naics: list[str] = []
    set_asides: list[str] = []
    agency_prefixes: list[str] = []
    deadline_within_days: int | None = None

    @field_validator("query", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("deadline_within_days", mode="before")
    @classmethod
    def _zero_is_unset(cls, value: object) -> object:
        return None if value in (0, "") else value


def render_search(name: str, doc: SearchDocument) -> str:
    return f"""# saved search {_s(name)}: FTS5 text plus filters. Empty means any.
query = {_s(doc.query)}
naics = {_list(doc.naics)}
set_asides = {_list(doc.set_asides)}
agency_prefixes = {_list(doc.agency_prefixes)}        # path prefixes, matched on dot boundaries
deadline_within_days = {doc.deadline_within_days if doc.deadline_within_days else 0}
"""
