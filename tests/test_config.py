import re
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
    assert settings.usaspending_base_url == "https://api.usaspending.gov"
    assert settings.embed_base_url == "http://localhost:11434/v1"
    assert settings.embed_model == "nomic-embed-text"
    assert settings.embed_api_key is None
    assert settings.embed_batch_size == 32
    assert settings.ai_fast_provider == "openai"
    assert settings.ai_fast_base_url == "http://localhost:11434/v1"
    assert settings.ai_fast_model == "qwen3:14b"
    assert settings.ai_fast_api_key is None and settings.ai_fast_context_chars == 48_000
    assert settings.ai_deep_provider is None and settings.ai_deep_model is None
    assert settings.ai_timeout == 600.0


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


EXAMPLE = Path(__file__).parents[1] / ".env.example"


def test_env_example_lists_every_setting() -> None:
    names = re.findall(r"^MENTOR_([A-Z_]+)=", EXAMPLE.read_text(), flags=re.MULTILINE)
    assert len(names) == len(set(names))
    assert {name.lower() for name in names} == set(Settings.model_fields)


def test_env_example_is_the_defaults() -> None:
    assert Settings(_env_file=EXAMPLE).model_dump() == Settings(_env_file=None).model_dump()
