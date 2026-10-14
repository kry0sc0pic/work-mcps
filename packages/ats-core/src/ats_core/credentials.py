"""Per-site credential storage. Secrets go to the OS keychain; a non-secret index lists what's saved."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import keyring
from keyring.errors import PasswordDeleteError
from pydantic import BaseModel

SERVICE = "work-mcps"


def default_config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "work-mcps"


class Credential(BaseModel):
    username: str
    password: str


class CredentialStore:
    """Credentials for one platform, keyed by an account id (for Workday, the careers-site host)."""

    def __init__(self, platform: str, config_dir: Path | None = None):
        self.platform = platform
        self._index = (config_dir or default_config_dir()) / f"{platform}-accounts.json"

    def _key(self, account: str) -> str:
        return f"{self.platform}:{account}"

    def _read_index(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self._index.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, account: str, cred: Credential, meta: dict[str, str] | None = None) -> None:
        keyring.set_password(SERVICE, self._key(account), cred.model_dump_json())
        index = self._read_index()
        added_at = index.get(account, {}).get("added_at") or datetime.now(UTC).date().isoformat()
        index[account] = {**(meta or {}), "username": cred.username, "added_at": added_at}
        self._write_index(index)

    def _write_index(self, index: dict[str, dict[str, str]]) -> None:
        self._index.parent.mkdir(parents=True, exist_ok=True)
        self._index.write_text(json.dumps(index, indent=2))

    def update_meta(self, account: str, **fields: str) -> bool:
        """Change non-secret index fields (e.g. company). False if the account isn't saved."""
        index = self._read_index()
        if account not in index:
            return False
        index[account].update(fields)
        self._write_index(index)
        return True

    def get(self, account: str) -> Credential | None:
        raw = keyring.get_password(SERVICE, self._key(account))
        return Credential.model_validate_json(raw) if raw else None

    def delete(self, account: str) -> bool:
        index = self._read_index()
        existed = index.pop(account, None) is not None
        try:
            keyring.delete_password(SERVICE, self._key(account))
        except PasswordDeleteError:
            pass
        self._write_index(index)
        return existed

    def list(self) -> dict[str, dict[str, str]]:
        """account -> non-secret metadata (always includes username). Never returns passwords."""
        return self._read_index()
