import sqlite3
from collections.abc import Callable

from orrery.config import Settings
from orrery.extract.text import ExtractResult, extract_pending

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

    from orrery.progress import JobCancelled

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


def test_a_deck_is_unsupported_and_is_not_mistaken_for_a_document_or_a_workbook(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched
) -> None:
    """.docx, .xlsx and .pptx are all zips; the member list is the only trustworthy signal."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    pptx_id = fetched("briefing.pptx", buffer.getvalue())

    extract_pending(conn, settings)

    assert statuses(conn)[pptx_id] == ("unsupported", None)


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


MakeXlsx = Callable[..., bytes]


def test_extracts_a_workbook_with_one_block_per_sheet(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_xlsx: MakeXlsx
) -> None:
    """A sheet takes the position a PDF page takes, so the search layer can already point at
    it, and the sheet's name travels with its text."""
    from datetime import datetime

    book_id = fetched(
        "qa.xlsx",
        make_xlsx(
            {
                "Q&A": [
                    [None, "Question", "Answer"],
                    [1, "Is there an incumbent?", "No incumbent Contractor."],
                ],
                "CLINs": [["CLIN", "POP Begin"], ["0001", datetime(2026, 9, 15)]],
            }
        ),
    )

    extract_pending(conn, settings)

    status, text = statuses(conn)[book_id]
    assert status == "done"
    assert text is not None
    first, second = text.split("\f")
    assert first.splitlines()[0] == "Q&A"
    assert "\tQuestion\tAnswer" in first
    assert "1\tIs there an incumbent?\tNo incumbent Contractor." in first
    assert second.splitlines()[0] == "CLINs"


def test_a_date_with_no_time_is_written_as_a_plain_date(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_xlsx: MakeXlsx
) -> None:
    """Spreadsheets store a date as a datetime; the midnight is noise in a period of
    performance and would otherwise reach the search index."""
    from datetime import datetime

    book_id = fetched(
        "pop.xlsx",
        make_xlsx({"S": [[datetime(2027, 9, 14), datetime(2027, 9, 14, 17, 30)]]}),
    )

    extract_pending(conn, settings)

    _, text = statuses(conn)[book_id]
    assert text is not None
    assert "2027-09-14\t2027-09-14 17:30:00" in text


def test_blank_rows_are_dropped_and_interior_gaps_are_kept(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_xlsx: MakeXlsx
) -> None:
    book_id = fetched(
        "sparse.xlsx",
        make_xlsx({"S": [["a", None, "c", None, None], [None, None, None], ["d"]]}),
    )

    extract_pending(conn, settings)

    _, text = statuses(conn)[book_id]
    assert text is not None
    assert text.splitlines()[1:] == ["a\t\tc", "d"]


def test_an_empty_workbook_is_done_with_empty_text(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched, make_xlsx: MakeXlsx
) -> None:
    book_id = fetched("blank.xlsx", make_xlsx({"Sheet1": []}))

    extract_pending(conn, settings)

    assert statuses(conn)[book_id] == ("done", "")


def test_a_corrupt_workbook_is_failed_not_unsupported(
    conn: sqlite3.Connection, settings: Settings, fetched: Fetched
) -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "not xml at all <<<")
    bad_id = fetched("broken.xlsx", buffer.getvalue())

    extract_pending(conn, settings)

    assert statuses(conn)[bad_id] == ("failed", None)
