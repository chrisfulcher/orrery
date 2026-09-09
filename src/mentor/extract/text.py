"""Text extraction for fetched attachments. Spends no quota. Serves docs/DESIGN.md §8.

The file type is sniffed from the first bytes: Content-Type from SAM.gov is always
``application/octet-stream`` and filenames are not trustworthy. PDFs are read with pypdf and
stored as plain text with pages joined by a form feed, which lets a later step recover page
numbers by counting separators. Word documents are read with python-docx in document order,
paragraphs and tables together, because a solicitation's Section L and M instructions are
routinely laid out as tables and dropping them would lose the part a bidder needs most; a
.docx has no pages, so no separators are written. A file that cannot be read is recorded as
``failed`` and, like fetch failures, never retried automatically.

Measured over one NAICS slice on 2026-09-09 (docs/notes/sam-manifest-probe.md), attachment
types ran 70% PDF, 22% .docx and 5% .xlsx, so these two readers cover roughly 92% of files.
Spreadsheets are still ``unsupported``.
"""

import logging
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from mentor.config import Settings
from mentor.progress import Cancelled, Report, check, never, quiet

# pypdf warns at length about odd files; a file it cannot read is recorded as failed.
logging.getLogger("pypdf").setLevel(logging.ERROR)

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"  # .docx, .xlsx and .pptx are all zips; the members tell them apart
DOCX_MEMBER = "word/document.xml"
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
    """(status, text). A file with no extractable text -- a scanned PDF, an empty document --
    is done with empty text, which is the queue a later OCR step will read."""
    try:
        data = file.read_bytes()
        if data.startswith(PDF_MAGIC):
            text = _pdf_text(data)
        elif data.startswith(ZIP_MAGIC) and _is_docx(data):
            text = _docx_text(data)
        else:
            return "unsupported", None
    except Exception:  # pypdf and python-docx both raise more than their own errors
        return "failed", None
    return "done", text if text.strip() else ""


def _pdf_text(data: bytes) -> str:
    pages = PdfReader(BytesIO(data)).pages
    return PAGE_SEPARATOR.join(page.extract_text() for page in pages)


def _is_docx(data: bytes) -> bool:
    """A Word document rather than a spreadsheet or a deck, which are zips of the same shape."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            return DOCX_MEMBER in archive.namelist()
    except zipfile.BadZipFile:
        return False


def _docx_text(data: bytes) -> str:
    """Paragraphs and tables in document order. A table becomes one line per row, cells joined
    by tabs, so a Section L cross-walk still reads as rows rather than a column of fragments."""
    document = Document(BytesIO(data))
    lines = []
    for block in _blocks(document):
        if isinstance(block, Paragraph):
            lines.append(block.text)
        else:
            lines += [
                "\t".join(cell.text.replace("\n", " ").strip() for cell in row.cells)
                for row in block.rows
            ]
    return "\n".join(lines)


def _blocks(document: Document) -> Iterator[Paragraph | Table]:
    """Body children in the order they appear; python-docx exposes paragraphs and tables as
    two separate lists, which loses their interleaving."""
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)
