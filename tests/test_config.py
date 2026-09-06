from pathlib import Path

import pytest

from mentor.config import Settings


def test_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.data_dir == Path("data")
    assert settings.db_path == Path("data") / "mentor.sqlite"
    assert settings.sam_api_key is None
    assert settings.sam_daily_budget == 10
    assert settings.sam_base_url == "https://api.sam.gov"
    assert settings.naics == []


def test_naics_is_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENTOR_NAICS", "541512, 541511,")
    assert Settings(_env_file=None).naics == ["541512", "541511"]


def test_data_dir_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MENTOR_DATA_DIR", str(tmp_path))
    assert Settings(_env_file=None).db_path == tmp_path / "mentor.sqlite"


def test_api_key_is_read_but_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENTOR_SAM_API_KEY", "hunter2")
    settings = Settings(_env_file=None)
    assert settings.sam_api_key is not None
    assert settings.sam_api_key.get_secret_value() == "hunter2"
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)
    assert "hunter2" not in settings.model_dump_json()
