"""FAR and DFARS clause references, read out of the text orrery already has (DESIGN.md §9
step 3, §2 provenance).

A solicitation says what it is by the clauses it incorporates: 52.219-14 is a limitation on
subcontracting, 252.204-7012 is safeguarding covered defense information, and a Section I
list is the cheapest description of the deal on offer. The references are in the description
and the attachments already extracted, so reading them costs a regex over text the store
holds; nothing is downloaded, nothing new is stored, and a reference found today is found
again tomorrow from the same bytes.

The number is all this module resolves. What a clause *says* needs the clause text, which is
either a FAR 52 / DFARS 252 corpus vendored into the package or an outbound endpoint the
zero-telemetry rule would have to admit, and that is a decision of its own: ``ClauseRef.title``
is the field it would fill and stays None until it is made (issue #21).

Every regulation in the FAR system keeps its solicitation provisions and contract clauses in
part 52 of its own chapter, so a clause part always ends in 52: 52 is the FAR's, 252 is the
defense supplement's, and 1852 is NASA's, 3052 is DHS's, 5652 is SOCOM's. That is what tells
a clause from the rest of the regulation, which is cited in exactly the same shape and is not
a clause: ``FAR 15.404-1(b)`` is a price analysis technique and ``FAR 28.307-2(b)`` is an
insurance rule, and on the live store those two and ``16.301-3`` were three of the ten most
matched numbers before this narrowing. A supplement part orrery does not recognise is still a
clause and passes through labelled by its part, because an unknown chapter is a regulation
orrery has not been taught, not an error in the document.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

CLAUSE_RE = re.compile(
    # Not after a word character, a dot, or a hyphen, so a version string (v1.204-3) and the
    # tail of a longer number are not clause references.
    r"(?<![\w.\-])"
    r"(?:(?:FAR|DFARS)\s+)?"
    # 52.204-21: the part is a chapter's clause part, two to four digits ending in 52, so
    # 5.204-21 and 15.404-1 are not clauses; the subpart is exactly three digits and the
    # clause one to four. Not before a digit or a dotted digit, so a decimal or a date tail
    # cannot extend the number, while a sentence's full stop can still follow it.
    r"(?P<part>\d{0,2}52)\.(?P<subpart>\d{3})-(?P<clause>\d{1,4})(?![\d]|\.\d|-\d)"
    # A class deviation changes the text, not which clause is cited; it is consumed so it
    # cannot be read as part of the number, and then dropped.
    r"(?:\s*\((?:DEVIATION|CLASS\s+DEVIATION)[^)]*\))?"
    r"(?:\s+Alt(?:ernate)?\.?\s+(?P<alt>[IVX]{1,4})\b)?",
    re.IGNORECASE,
)
"""One clause reference. The optional ``FAR``/``DFARS`` prefix is tolerated and ignored: the
part says which regulation it is, and a document that writes ``DFARS 52.204-21`` is wrong
about itself."""

REGULATIONS = {"52": "FAR", "252": "DFARS"}

ORDER = {"FAR": 0, "DFARS": 1, "supplement": 2}
"""The order the panels read in: the FAR everyone shares, then the defense supplement, then
whatever agency supplement this buy adds."""


def regulation(part: str) -> str:
    """Which regulation a part belongs to. Anything but 52 and 252 is an agency supplement,
    named as one rather than mislabelled FAR."""
    return REGULATIONS.get(part, "supplement")


@dataclass(frozen=True)
class ClauseRef:
    number: str
    """``52.204-21``, with `` Alt I`` appended when the citation carries an alternate."""
    regulation: str
    """``FAR``, ``DFARS``, or ``supplement``."""
    mentions: int
    """How many times it is cited, across every document scanned."""
    attachments: tuple[str, ...]
    """The documents citing it, in the order they were scanned; ``notice`` for the notice's
    own description, as a search hit's source names it."""
    title: str | None = None
    """What the clause says it is. Always None today; see the module docstring."""


def _number(match: re.Match[str]) -> str:
    """The citation as a key: the three numbers, and an alternate normalised to ``Alt II`` so
    ``Alternate II`` and ``Alt. II`` are the one clause they are."""
    number = f"{match['part']}.{match['subpart']}-{match['clause']}"
    return f"{number} Alt {match['alt'].upper()}" if match["alt"] else number


def find(text: str) -> Counter[str]:
    """Every clause cited in one document, counted."""
    return Counter(_number(match) for match in CLAUSE_RE.finditer(text))


def _sort_key(number: str) -> tuple[int, int, int, int, str]:
    head, _, alt = number.partition(" ")
    part, rest = head.split(".", 1)
    subpart, clause = rest.split("-", 1)
    return (ORDER[regulation(part)], int(part), int(subpart), int(clause), alt)


def references(texts: Iterable[tuple[str, str]]) -> tuple[ClauseRef, ...]:
    """The clauses cited across ``(document, text)`` pairs, FAR first, then DFARS, then the
    supplements, each part in numeric order so 52.204-7 comes before 52.204-21. A document
    that cites a clause ten times is named once and counts ten."""
    mentions: Counter[str] = Counter()
    where: dict[str, list[str]] = {}
    for document, text in texts:
        if not text:
            continue
        for number, count in find(text).items():
            mentions[number] += count
            where.setdefault(number, []).append(document)
    return tuple(
        ClauseRef(
            number=number,
            regulation=regulation(number.split(".", 1)[0]),
            mentions=mentions[number],
            attachments=tuple(where[number]),
        )
        for number in sorted(mentions, key=_sort_key)
    )
