from pathlib import Path

import pytest

from mentor.config import Settings


def test_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.data_dir == Path("data")
    assert settings.db_path == Path("data") / "mentor.sqlite"


def test_data_dir_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MENTOR_DATA_DIR", str(tmp_path))
    assert Settings(_env_file=None).db_path == tmp_path / "mentor.sqlite"
