"""Text extraction for fetched attachments. Spends no quota. Serves docs/DESIGN.md §8.

The file type is sniffed from the first bytes: Content-Type from SAM.gov is always
``application/octet-stream`` and filenames are not trustworthy. PDFs are read with pypdf and
stored as plain text with pages joined by a form feed, which lets a later step recover page
numbers by counting separators. Word documents are read with python-docx in document order,
paragraphs and tables together, because a solicitation's Section L and M instructions are
routinely laid out as tables and dropping them would lose the part a bidder needs most; a
.docx has no pages, so no separators are written. Workbooks are read with openpyxl, one
sheet per form-feed block headed by the sheet's name, so a sheet lands where a PDF page
would and the search layer can already point at it. A file that cannot be read is recorded
as ``failed`` and, like fetch failures, never retried automatically.

Measured over one NAICS slice on 2026-09-09 (docs/notes/sam-manifest-probe.md), attachment
types ran 70% PDF, 22% .docx and 5% .xlsx, so these three readers cover roughly 97% of
files. The spreadsheets are not the pricing grids the share suggests: the sample held Q&A
logs answering who the incumbent is, self-scoring worksheets stating how an offer will be
evaluated, CLIN structures, and questionnaires. Blank price templates are the low-value
case and cost little, since only the cells a workbook actually stores are read.
"""

import logging
import sqlite3
import warnings
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path

import openpyxl
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from orrery.config import Settings
from orrery.progress import Cancelled, Report, check, never, quiet

# pypdf warns at length about odd files; a file it cannot read is recorded as failed.
logging.getLogger("pypdf").setLevel(logging.ERROR)
# openpyxl warns about extensions it drops (data validation, conditional formatting); none of
# them carry text, so the warning says nothing about the extraction.
warnings.filterwarnings("ignore", module="openpyxl")

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"  # .docx, .xlsx and .pptx are all zips; the members tell them apart
DOCX_MEMBER = "word/document.xml"
XLSX_MEMBER = "xl/workbook.xml"  # .xlsx and macro-enabled .xlsm alike; .xls is not a zip
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
        elif data.startswith(ZIP_MAGIC) and _zip_has(data, DOCX_MEMBER):
            text = _docx_text(data)
        elif data.startswith(ZIP_MAGIC) and _zip_has(data, XLSX_MEMBER):
            text = _xlsx_text(data)
        else:
            return "unsupported", None
    except Exception:  # pypdf and python-docx both raise more than their own errors
        return "failed", None
    return "done", text if text.strip() else ""


def _pdf_text(data: bytes) -> str:
    pages = PdfReader(BytesIO(data)).pages
    return PAGE_SEPARATOR.join(page.extract_text() for page in pages)


def _zip_has(data: bytes, member: str) -> bool:
    """Which office format this is. A document, a workbook and a deck are all zips, so the
    member list is the only trustworthy signal."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            return member in archive.namelist()
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


def _xlsx_text(data: bytes) -> str:
    """One form-feed block per sheet that holds anything, headed by the sheet's name, so a
    sheet occupies the position a PDF page would and the name travels with the text.

    ``data_only`` reads the values a spreadsheet caches rather than its formulas: ``=SUM(...)``
    is not what a reader is searching for, and a formula whose result was never cached is
    simply an empty cell. ``read_only`` matters more than it looks -- these sheets are mostly
    empty (one price template declared 932 by 16,383 cells and stored 148 values), and
    read-only mode walks the rows the file actually holds rather than the declared extent.
    """
    workbook = openpyxl.load_workbook(BytesIO(data), data_only=True, read_only=True)
    try:
        blocks = []
        for sheet in workbook.worksheets:
            rows = [line for row in sheet.iter_rows(values_only=True) if (line := _row(row))]
            if rows:
                blocks.append("\n".join([sheet.title, *rows]))
    finally:
        workbook.close()
    return PAGE_SEPARATOR.join(blocks)


def _row(values: tuple) -> str:
    """A row as tab-joined cells, empty if it holds nothing. Trailing blanks are dropped and
    interior ones kept, so a gap between two columns still reads as a gap."""
    cells = [_cell(value) for value in values]
    while cells and not cells[-1]:
        cells.pop()
    return "\t".join(cells) if any(cells) else ""


def _cell(value: object) -> str:
    """A cell as text. A date that carries no time is written as a plain date: spreadsheets
    store both as datetimes, and `2027-09-14 00:00:00` is noise in a period of performance."""
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time.min else value.isoformat(" ")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value).strip()


def _blocks(document: Document) -> Iterator[Paragraph | Table]:
    """Body children in the order they appear; python-docx exposes paragraphs and tables as
    two separate lists, which loses their interleaving."""
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)
