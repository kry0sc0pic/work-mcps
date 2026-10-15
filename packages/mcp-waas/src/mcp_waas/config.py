from pathlib import Path

from ats_core import default_cache_dir
from pydantic_settings import BaseSettings, SettingsConfigDict


class WaasSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WAAS_", env_file=".env", extra="ignore")

    cache_dir: Path = default_cache_dir() / "waas"
    # Optional: config dir override for the account index (defaults to ~/.config/work-mcps).
    config_dir: Path | None = None
    # Cached applications newer than this are served without asking the site again.
    cache_ttl_minutes: int = 30

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "waas.db"

    def snapshot_path(self, account_key: str) -> Path:
        return self.cache_dir / f"snapshot-{account_key}.json"

    def session_path(self, account_key: str) -> Path:
        return self.cache_dir / f"session-{account_key}.json"
