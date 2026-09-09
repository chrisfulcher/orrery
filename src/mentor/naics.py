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

from collections.abc import Iterable

MIN_LENGTH = 2  # a sector
MAX_LENGTH = 6  # a national industry


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


__all__ = ["MAX_LENGTH", "MIN_LENGTH", "match_sql", "matches", "validate_slice"]
