import errno
import os
import stat
import tempfile
from pathlib import Path

import pytest

from mentor import dotenv
from mentor.config import Settings, env_key, env_values, environment_overrides, setup_needed


def test_every_setting_round_trips_through_the_file(tmp_path: Path) -> None:
    original = Settings(
        _env_file=None,
        sam_api_key="sk 1 with spaces#and hash",
        naics=["541512", "541511"],
        ai_deep_provider="anthropic",
        ai_deep_api_key='k"quoted"',
        ai_timeout=42.5,
        data_dir=tmp_path / "store",
    )
    path = tmp_path / ".env"
    dotenv.write(path, env_values(original))
    reloaded = Settings(_env_file=path)
    assert reloaded.model_dump(exclude={"sam_api_key", "ai_deep_api_key"}) == original.model_dump(
        exclude={"sam_api_key", "ai_deep_api_key"}
    )
    assert reloaded.sam_api_key.get_secret_value() == "sk 1 with spaces#and hash"
    assert reloaded.ai_deep_api_key.get_secret_value() == 'k"quoted"'
    assert set(dotenv.read(path)) == {env_key(f) for f in Settings.model_fields}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_keeps_comments_order_and_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# SAM.gov access\nMENTOR_SAM_API_KEY=old  # inline\n\nFOO=bar\n"
        "export MENTOR_NAICS=541512\n"
    )
    dotenv.write(
        path, {"MENTOR_SAM_API_KEY": "new", "MENTOR_NAICS": None, "MENTOR_FETCH_DELAY": "2"}
    )
    assert path.read_text() == (
        "# SAM.gov access\nMENTOR_SAM_API_KEY=new\n\nFOO=bar\nMENTOR_NAICS=\n\n"
        "MENTOR_FETCH_DELAY=2\n"
    )
    assert dotenv.read(path) == {
        "MENTOR_SAM_API_KEY": "new",
        "FOO": "bar",
        "MENTOR_NAICS": "",
        "MENTOR_FETCH_DELAY": "2",
    }


def test_write_creates_the_file_and_rejects_other_keys(tmp_path: Path) -> None:
    path = tmp_path / "sub" / ".env"
    dotenv.write(path, {"MENTOR_NAICS": "541512"})
    assert path.read_text() == "MENTOR_NAICS=541512\n"
    with pytest.raises(ValueError, match="only MENTOR_"):
        dotenv.write(path, {"ANTHROPIC_API_KEY": "x"})
    with pytest.raises(ValueError, match="directory"):
        dotenv.write(tmp_path, {"MENTOR_NAICS": "1"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("plain", "plain"), ("http://x:1/v1", "http://x:1/v1"), ("", ""), (None, ""),
     ("a b", '"a b"'), ("x#y", '"x#y"'), ("it's", "\"it's\""), ('q"q', '"q\\"q"')],
)  # fmt: skip
def test_quote(value: str | None, expected: str) -> None:
    assert dotenv.quote(value) == expected


def test_read_unquotes_and_drops_inline_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text('A=\'single quoted\'\nB="double \\"q\\""\nC=bare value # comment\n#D=hidden\n')
    assert dotenv.read(path) == {"A": "single quoted", "B": 'double "q"', "C": "bare value"}
    assert dotenv.read(tmp_path / "missing") == {}


def test_write_falls_back_to_in_place_on_a_busy_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("MENTOR_NAICS=1\n")
    real = os.replace

    def busy(src: str, dst: str) -> None:
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(os, "replace", busy)
    dotenv.write(path, {"MENTOR_NAICS": "2"})
    monkeypatch.setattr(os, "replace", real)
    assert path.read_text() == "MENTOR_NAICS=2\n" and list(tmp_path.glob(".env.*")) == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_rewrites_in_place_when_the_directory_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container: /app belongs to root, .env is a bind mount the app user may write."""
    path = tmp_path / ".env"
    path.write_text("# kept\nMENTOR_NAICS=1\n")

    def denied(**kwargs: object) -> tuple[int, str]:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", denied)
    dotenv.write(path, {"MENTOR_NAICS": "2", "MENTOR_FETCH_DELAY": "0.5"})
    assert path.read_text() == "# kept\nMENTOR_NAICS=2\n\nMENTOR_FETCH_DELAY=0.5\n"
    assert list(tmp_path.glob(".env.*")) == []


def test_environment_overrides_and_setup_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith("MENTOR_"):
            monkeypatch.delenv(name)
    assert environment_overrides() == set()
    monkeypatch.setenv("MENTOR_SAM_DAILY_BUDGET", "99")
    monkeypatch.setenv("MENTOR_NAICS", "")
    assert environment_overrides() == {"sam_daily_budget"}

    env = tmp_path / ".env"
    # The key is not an essential: without one the bulk extract still carries notices and
    # their descriptions, and attachments are read from URLs that take no key.
    assert setup_needed(Settings(_env_file=None), env) == "MENTOR_NAICS is empty"
    assert setup_needed(Settings(_env_file=None, sam_api_key="k"), env) == "MENTOR_NAICS is empty"
    ready = Settings(_env_file=None, naics=["541512"])
    assert setup_needed(ready, env) is None  # a variable in the environment counts as configured
    monkeypatch.delenv("MENTOR_SAM_DAILY_BUDGET")
    assert setup_needed(ready, env) == f"{env} not found"
    env.write_text("MENTOR_NAICS=541512\n")
    assert setup_needed(ready, env) is None and setup_needed(ready, None) is None
