"""Runtime configuration.

Every setting is read from the environment as ``MENTOR_<FIELD>`` (for example
``MENTOR_DATA_DIR``), or from a ``.env`` file in the working directory. Real
environment variables take precedence over ``.env``. Empty values count as unset.
Unknown keys in ``.env`` are ignored so that a file written for a newer version never
breaks an older one.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MENTOR_", env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    data_dir: Path = Path("data")
    """Directory holding the database and downloaded attachments."""

    sam_api_key: SecretStr | None = None
    """SAM.gov public API key. Never logged or stored; unwrapped only when a request is sent."""

    sam_daily_budget: int = 10
    """Keyed SAM.gov requests (search pages and descriptions) allowed per UTC day."""

    sam_base_url: str = "https://api.sam.gov"
    """The only host the API key is ever sent to."""

    naics: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """NAICS codes to ingest, comma-separated in MENTOR_NAICS. Empty means nothing to ingest."""

    fetch_delay: float = 1.0
    """Seconds to pause between attachment downloads (public files; politeness, not quota)."""

    max_attachment_bytes: int = 100 * 1024 * 1024
    """Attachments larger than this are recorded as skipped, not downloaded."""

    usaspending_base_url: str = "https://api.usaspending.gov"
    """USAspending API host: no key, no quota. The finished file is served from a sibling host
    under the same domain, which is the only other host this adapter contacts."""

    embed_base_url: str = "http://localhost:11434/v1"
    """OpenAI-compatible base URL; the only place embedding requests and their text go."""

    embed_model: str = "nomic-embed-text"
    """Embedding model name as the endpoint knows it; recorded on every stored vector."""

    embed_api_key: SecretStr | None = None
    """Sent as a Bearer token only when set. Never logged."""

    embed_batch_size: int = 32
    """Texts per embeddings request."""

    ai_fast_provider: Literal["openai", "anthropic"] = "openai"
    """The fast slot (grunt work): `openai` for any OpenAI-compatible endpoint, `anthropic`."""

    ai_fast_base_url: str = "http://localhost:11434/v1"
    """Where fast-slot requests and their text go; a local Ollama by default."""

    ai_fast_model: str = "qwen3:14b"
    """Model name as the endpoint knows it (`ollama pull qwen3:14b`)."""

    ai_fast_api_key: SecretStr | None = None
    """Sent only to the fast slot's endpoint. Never logged."""

    ai_fast_context_chars: int = 48_000
    """Prompt budget for the fast slot, in characters (about 12k tokens)."""

    ai_deep_provider: Literal["openai", "anthropic"] | None = None
    """The deep slot (judgment). Unset: the deep slot is the fast slot."""

    ai_deep_base_url: str | None = None
    """Unset: the SDK's own default for anthropic, the fast base URL for openai."""

    ai_deep_model: str | None = None
    """Unset: claude-opus-5 for anthropic; required for openai."""

    ai_deep_api_key: SecretStr | None = None
    """Unset with anthropic: the SDK reads ANTHROPIC_API_KEY from the environment."""

    ai_deep_context_chars: int | None = None
    """Unset: the fast slot's budget."""

    ai_timeout: float = 600.0
    """Seconds to wait for one model response; local models can take minutes."""

    @field_validator("naics", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [code.strip() for code in value.split(",") if code.strip()]
        return value

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mentor.sqlite"
