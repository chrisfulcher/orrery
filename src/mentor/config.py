"""Runtime configuration.

Every setting is read from the environment as ``MENTOR_<FIELD>`` (for example
``MENTOR_DATA_DIR``), or from a ``.env`` file in the working directory. Real
environment variables take precedence over ``.env``. Unknown keys in ``.env``
are ignored so that a file written for a newer version never breaks an older one.
"""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MENTOR_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    """Directory holding the database and downloaded attachments."""

    sam_api_key: SecretStr | None = None
    """SAM.gov public API key. Never logged or stored; unwrapped only when a request is sent."""

    sam_daily_budget: int = 10
    """Keyed SAM.gov requests (search pages and descriptions) allowed per UTC day."""

    sam_base_url: str = "https://api.sam.gov"
    """The only host the API key is ever sent to."""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mentor.sqlite"
