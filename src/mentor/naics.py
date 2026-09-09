"""The NAICS ingestion slice: prefix matching, the way the sources do it.

``MENTOR_NAICS`` holds NAICS *prefixes*, not complete codes. USAspending's award API filters
the same way -- ``5415`` selects every code beneath that industry group, and a longer prefix
wins over a shorter one -- so mentor matches prefixes everywhere it filters by NAICS. The
Python and SQL forms below are the only two implementations, so the adapters and the query
module cannot drift apart. A six-digit element matches its own code and nothing else, which
is what every slice meant before this module existed.

Nothing here knows about NAICS revisions. A prefix survives a renumbering only when the
revision stays inside it, and that is not guaranteed: the 2022 revision moved wired telecom
from 517311 to 517111, across the four-digit boundary. Vintage lineage is issue #2.
"""

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime

from mentor.progress import Report

MIN_LENGTH = 2  # a sector
MAX_LENGTH = 6  # a national industry

VINTAGE = 2022
"""The NAICS revision this release is written against."""

REVISION_YEARS = 5
"""Census revises NAICS on a five-year cycle."""


def validate_slice(codes: Iterable[str], *, what: str = "NAICS code") -> list[str]:
    """Each element stripped, blanks dropped, and checked: digits only, two to six of them.
    Raises ``ValueError`` naming the offending value."""
    cleaned = []
    for value in codes:
        code = value.strip()
        if not code:
            continue
        if not code.isdigit() or not MIN_LENGTH <= len(code) <= MAX_LENGTH:
            raise ValueError(f"{what} {code!r} is not {MIN_LENGTH} to {MAX_LENGTH} digits")
        cleaned.append(code)
    return cleaned


def matches(code: str | None, prefixes: Iterable[str]) -> bool:
    """Whether ``code`` falls in the slice: it starts with one of ``prefixes``. An empty
    slice matches nothing, as an empty ``MENTOR_NAICS`` means nothing to ingest."""
    if not code:
        return False
    return any(code.startswith(prefix) for prefix in prefixes)


def match_sql(column: str, param: str) -> str:
    """The SQL form of ``matches``: true when ``column`` starts with any element of the JSON
    array bound to ``param``. ``substr`` rather than ``LIKE`` so that no element of the slice
    can be read as a wildcard pattern. Callers keep their own null guard on ``param``."""
    return (
        f"EXISTS (SELECT 1 FROM json_each({param})"
        f" WHERE substr({column}, 1, length(value)) = value)"
    )


def vintage_warning(today: date | None = None) -> str | None:
    """A line worth printing once the next revision is due, or None. The calendar is the only
    signal: no network call, and no false alarm on a slice that is merely quiet."""
    today = today or datetime.now(UTC).date()
    due = VINTAGE + REVISION_YEARS
    if today.year < due:
        return None
    return (
        f"NAICS {due} is expected to be in force; this release reads codes as of NAICS "
        f"{VINTAGE}. A code renumbered since then selects nothing -- see issue #2."
    )


class SliceTally:
    """How many rows each element of the slice took. An element that ends a run on zero is
    named in the summary: on its own, a zero-row run cannot tell a quiet market from a code
    that no longer selects anything, and that is the whole of issue #6."""

    def __init__(self, prefixes: Iterable[str]) -> None:
        self.counts = dict.fromkeys(prefixes, 0)

    def take(self, code: str | None) -> bool:
        """Whether ``code`` is in the slice, counted against every element that took it. A row
        selected some other way (a known vendor by UEI, say) should not be offered here."""
        hit = False
        for prefix in self.counts:
            if matches(code, (prefix,)):
                self.counts[prefix] += 1
                hit = True
        return hit

    def empty(self) -> list[str]:
        """The elements that took nothing, in configured order."""
        return [prefix for prefix, taken in self.counts.items() if not taken]

    def as_json(self) -> str:
        """The run record: the slice as sent and what each element took."""
        return json.dumps({"naics": self.counts}, sort_keys=True)


def report_empty(tally: SliceTally, report: Report, *, rows_read: int) -> None:
    """Name every slice element that took nothing. A report, never a failure: a resumed run
    legitimately reads no new rows, so a check that raised here would fire on every resume.
    For the same reason a run that read nothing at all says nothing -- it has no evidence
    about the slice either way, and naming every element there would be a lie about a file
    the run never reached."""
    if not rows_read:
        return
    for prefix in tally.empty():
        report(f"NAICS {prefix} matched nothing in this file")


__all__ = [
    "MAX_LENGTH",
    "MIN_LENGTH",
    "REVISION_YEARS",
    "VINTAGE",
    "SliceTally",
    "match_sql",
    "report_empty",
    "matches",
    "validate_slice",
    "vintage_warning",
]
