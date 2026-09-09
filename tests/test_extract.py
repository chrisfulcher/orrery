import sqlite3
from collections.abc import Callable

from mentor.config import Settings
from mentor.extract.text import ExtractResult, extract_pending

Fetched = Callable[..., int]
MakePdf = Callable[[list[str]], bytes]


def statuses(conn: sqlite3.Connection) -> dict[int, tuple[str, str | None]]:
    rows = conn.execute("SELECT attachment_id, extract_status, extracted_text FROM attachments")
    return {row[0]: (row[1], row[2]) for row in rows}


def test_extracts_pdf_and_records_other_outcomes(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_pdf: MakePdf
) -> None:
    pdf_id = fetched(
        "sow.pdf", make_pdf(["Section L instructions for wi-fi", "Statement of work page two"])
    )
    other_id = fetched("notes.txt", b"not a pdf")
    corrupt_id = fetched("broken.pdf", b"%PDF-1.4 doc one")

    result = extract_pending(conn, settings)

    assert result == ExtractResult(done=1, unsupported=1, failed=1)
    by_id = statuses(conn)
    assert by_id[pdf_id] == ("done", "Section L instructions for wi-fi\fStatement of work page two")
    assert by_id[other_id] == ("unsupported", None)
    assert by_id[corrupt_id] == ("failed", None)
    hits = conn.execute(
        "SELECT rowid FROM attachments_fts WHERE attachments_fts MATCH 'two'"
    ).fetchall()
    assert hits == [(pdf_id,)]
    (untouched,) = conn.execute(
        "SELECT count(*) FROM attachments"
        " WHERE fetch_status = 'pending' AND extract_status = 'pending'"
    ).fetchone()
    assert untouched == 17


def test_limit_and_idempotence(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_pdf: MakePdf
) -> None:
    fetched("a.pdf", make_pdf(["alpha"]))
    fetched("b.pdf", make_pdf(["beta"]))

    assert extract_pending(conn, settings, limit=1) == ExtractResult(1, 0, 0)
    assert extract_pending(conn, settings) == ExtractResult(1, 0, 0)
    assert extract_pending(conn, settings) == ExtractResult(0, 0, 0)


def test_scanned_pdf_is_done_with_empty_text(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_pdf: MakePdf
) -> None:
    attachment_id = fetched("scan.pdf", make_pdf(["", ""]))

    assert extract_pending(conn, settings) == ExtractResult(1, 0, 0)
    assert statuses(conn)[attachment_id] == ("done", "")


def test_extract_reports_and_cancels_between_files(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_pdf: MakePdf
) -> None:
    import pytest

    from mentor.progress import JobCancelled

    fetched("a.pdf", make_pdf(["one"]))
    fetched("b.pdf", make_pdf(["two"]))
    lines: list[str] = []
    with pytest.raises(JobCancelled):
        extract_pending(conn, settings, report=lines.append, cancelled=lambda: len(lines) >= 1)
    assert len(lines) == 1 and lines[0].startswith("done: attachments/")
    assert list(statuses(conn).values()).count(("done", "one")) == 1
    assert extract_pending(conn, settings) == ExtractResult(done=1, unsupported=0, failed=0)


MakeDocx = Callable[..., bytes]


def test_extracts_word_documents_in_document_order_with_their_tables(
    conn: sqlite3.Connection,
    settings: Settings,
    fetched: Fetched,
    make_docx: MakeDocx,
) -> None:
    """Section L and M instructions are routinely laid out as tables, so a reader that took
    paragraphs only would drop the part a bidder needs most (#11)."""
    docx_id = fetched(
        "sow.docx",
        make_docx(
            ["Section L instructions", "Section M evaluation"],
            table=[["Factor", "Weight"], ["Technical", "60"]],
        ),
    )

    result = extract_pending(conn, settings)

    status, text = statuses(conn)[docx_id]
    assert status == "done"
    assert text is not None
    assert text.index("Section L instructions") < text.index("Factor\tWeight")
    assert text.index("Factor\tWeight") < text.index("Section M evaluation")
    assert "Technical\t60" in text
    assert result.done >= 1


def test_a_spreadsheet_is_still_unsupported_not_mistaken_for_a_word_document(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched
) -> None:
    """.docx, .xlsx and .pptx are all zips; the members are what tell them apart."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    xlsx_id = fetched("prices.xlsx", buffer.getvalue())

    extract_pending(conn, settings)

    assert statuses(conn)[xlsx_id] == ("unsupported", None)


def test_an_empty_word_document_is_done_with_empty_text(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_docx: MakeDocx
) -> None:
    empty_id = fetched("blank.docx", make_docx([]))

    extract_pending(conn, settings)

    assert statuses(conn)[empty_id] == ("done", "")


def test_a_corrupt_word_document_is_failed_not_unsupported(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched
) -> None:
    """It sniffs as a docx and then will not open; that is a failure, not an unknown type."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "not xml at all <<<")
    bad_id = fetched("broken.docx", buffer.getvalue())

    extract_pending(conn, settings)

    assert statuses(conn)[bad_id] == ("failed", None)
