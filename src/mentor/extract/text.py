"""Text extraction for fetched attachments. Spends no quota. Serves docs/DESIGN.md §8.

The file type is sniffed from the first bytes: Content-Type from SAM.gov is always
``application/octet-stream`` and filenames are not trustworthy. PDFs are read with pypdf
and stored as plain text with pages joined by a form feed. A file that cannot be read is
recorded as ``failed`` and, like fetch failures, never retried automatically.
"""

import logging
import sqlite3
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from mentor.config import Settings
from mentor.progress import Cancelled, Report, check, never, quiet

# pypdf warns at length about odd files; a file it cannot read is recorded as failed.
logging.getLogger("pypdf").setLevel(logging.ERROR)

PDF_MAGIC = b"%PDF-"
PAGE_SEPARATOR = "\f"

PENDING = """
SELECT attachment_id, path FROM attachments
WHERE fetch_status = 'fetched' AND extract_status = 'pending'
ORDER BY attachment_id
LIMIT ?
"""


@dataclass(frozen=True)
class ExtractResult:
    done: int
    unsupported: int
    failed: int


def extract_pending(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    limit: int | None = None,
    report: Report = quiet,
    cancelled: Cancelled = never,
) -> ExtractResult:
    """Extract text from every fetched attachment not yet attempted."""
    counts = {"done": 0, "unsupported": 0, "failed": 0}
    rows = conn.execute(PENDING, (-1 if limit is None else limit,)).fetchall()
    for attachment_id, path in rows:
        check(cancelled)
        status, text = _extract(settings.data_dir / path)
        report(f"{status}: {path}")
        conn.execute(
            "UPDATE attachments SET extracted_text = ?, extract_status = ? WHERE attachment_id = ?",
            (text, status, attachment_id),
        )
        counts[status] += 1
    return ExtractResult(**counts)


def _extract(file: Path) -> tuple[str, str | None]:
    """(status, text). A PDF with no extractable text (scanned) is done with empty text."""
    try:
        data = file.read_bytes()
        if not data.startswith(PDF_MAGIC):
            return "unsupported", None
        pages = PdfReader(BytesIO(data)).pages
        text = PAGE_SEPARATOR.join(page.extract_text() for page in pages)
    except Exception:  # pypdf raises more than PdfReadError on odd files; a missing file too
        return "failed", None
    return "done", text if text.strip() else ""
