from pathlib import Path

from ats_core import default_cache_dir
from pydantic_settings import BaseSettings, SettingsConfigDict


class OracleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORACLE_", env_file=".env", extra="ignore")

    cache_dir: Path = default_cache_dir() / "oracle"
    # Optional: config dir override for the account index (defaults to ~/.config/work-mcps).
    config_dir: Path | None = None
    # Cached applications newer than this are served without logging in again.
    cache_ttl_minutes: int = 30

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "oracle.db"

    def snapshot_path(self, account_key: str) -> Path:
        return self.cache_dir / f"snapshot-{account_key}.json"

    def session_path(self, account_key: str) -> Path:
        return self.cache_dir / f"session-{account_key}.json"
