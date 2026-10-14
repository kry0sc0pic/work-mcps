from pathlib import Path

from ats_core import default_cache_dir
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkdaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKDAY_", env_file=".env", extra="ignore")

    cache_dir: Path = default_cache_dir() / "workday"
    # Optional: config dir override for the credential index (defaults to ~/.config/work-mcps).
    config_dir: Path | None = None
    # Cached applications newer than this are served without logging in again.
    cache_ttl_minutes: int = 30

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "workday.db"

    def snapshot_path(self, account_key: str) -> Path:
        return self.cache_dir / f"snapshot-{account_key}.json"

    def session_path(self, account_key: str) -> Path:
        return self.cache_dir / f"session-{account_key}.json"
