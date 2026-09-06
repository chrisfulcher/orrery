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
