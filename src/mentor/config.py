"""Runtime configuration.

Every setting is read from the environment as ``MENTOR_<FIELD>`` (for example
``MENTOR_DATA_DIR``), or from a ``.env`` file in the working directory. Real
environment variables take precedence over ``.env``. Unknown keys in ``.env``
are ignored so that a file written for a newer version never breaks an older one.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MENTOR_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    """Directory holding the database and downloaded attachments."""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mentor.sqlite"
