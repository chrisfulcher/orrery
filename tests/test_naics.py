import json
import sqlite3

import pytest

from mentor import naics


def test_a_six_digit_prefix_matches_only_itself() -> None:
    assert naics.matches("541512", ["541512"])
    assert not naics.matches("541511", ["541512"])


def test_a_shorter_prefix_takes_everything_beneath_it() -> None:
    assert naics.matches("541512", ["5415"])
    assert naics.matches("541330", ["54"])
    assert not naics.matches("541512", ["5416"])


def test_any_element_of_the_slice_can_match() -> None:
    assert naics.matches("236220", ["5415", "2362"])


def test_an_empty_slice_and_a_missing_code_match_nothing() -> None:
    assert not naics.matches("541512", [])
    assert not naics.matches(None, ["5415"])
    assert not naics.matches("", ["5415"])


def test_validate_slice_keeps_two_to_six_digits_and_drops_blanks() -> None:
    assert naics.validate_slice(["54", " 5415 ", "541512", ""]) == ["54", "5415", "541512"]


@pytest.mark.parametrize("code", ["5", "5415123", "54a", "54.15", "-1"])
def test_validate_slice_rejects_and_names_the_value(code: str) -> None:
    with pytest.raises(ValueError, match=repr(code.strip())):
        naics.validate_slice([code])


def test_match_sql_agrees_with_matches() -> None:
    """The two forms are the point of this module, so hold them to the same answers."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (naics_code TEXT)")
    codes = ["541512", "541511", "236220", None]
    conn.executemany("INSERT INTO t VALUES (?)", [(c,) for c in codes])
    sql = f"SELECT naics_code FROM t WHERE {naics.match_sql('naics_code', '?')}"
    for slice_ in (["541512"], ["5415"], ["54"], ["54", "2362"], []):
        rows = {row[0] for row in conn.execute(sql, (json.dumps(slice_),))}
        assert rows == {c for c in codes if naics.matches(c, slice_)}
    conn.close()


def test_match_sql_does_not_read_the_slice_as_a_wildcard() -> None:
    """A LIKE-based form would match everything on '%'; substr cannot."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (naics_code TEXT)")
    conn.execute("INSERT INTO t VALUES ('541512')")
    sql = f"SELECT count(*) FROM t WHERE {naics.match_sql('naics_code', '?')}"
    assert conn.execute(sql, ('["%"]',)).fetchone()[0] == 0
    assert conn.execute(sql, ('["_41512"]',)).fetchone()[0] == 0
    conn.close()
