"""Per-site credential storage. Secrets go to the OS keychain; a non-secret index lists what's saved."""

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import keyring
from keyring.errors import KeyringError, NoKeyringError, PasswordDeleteError
from pydantic import BaseModel

SERVICE = "work-mcps"


def default_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "work-mcps"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "work-mcps"


class CredentialStoreError(RuntimeError):
    """The system keychain is missing or refused a request. The message says what to do."""


def _no_backend_advice() -> str:
    if sys.platform.startswith("linux"):
        return (
            "On Linux this needs a Secret Service provider (GNOME Keyring or KWallet) running in a desktop "
            "session with D-Bus. Headless servers, containers and bare WSL usually have none; on those, set "
            "run `work-mcp headless` to use an encrypted file (or an explicit plaintext file) instead."
        )
    if sys.platform == "win32":
        return "Windows Credential Manager should be available; check that the process runs as a normal user."
    return "Check that the macOS Keychain is unlocked and that this process may access it."


_file_keyring_ready = False


def keyring_settings_path() -> Path:
    return default_config_dir() / "keyring.json"


def _keyring_choice() -> dict[str, str]:
    """The explicit headless choice: env var wins, else what `work-mcp headless` saved."""
    if mode := os.environ.get("WORK_MCP_KEYRING"):
        return {"backend": mode}
    try:
        return json.loads(keyring_settings_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _use_file_keyring_if_requested() -> None:
    """Swap the OS keychain for a file keyring, only when the user opted in (see filekeyring.py)."""
    global _file_keyring_ready
    choice = _keyring_choice()
    mode = choice.get("backend")
    if _file_keyring_ready or mode not in ("file", "plaintext"):
        return
    from ats_core.filekeyring import FileKeyringError, PlaintextKeyring, make_encrypted_keyring

    config = default_config_dir()
    try:
        if mode == "plaintext":
            if not sys.platform.startswith("linux"):
                raise CredentialStoreError("The plaintext keyring is only available on Linux.")
            kr = PlaintextKeyring(config / "credentials.json")
        else:
            kr = make_encrypted_keyring(config / "credentials.enc", choice.get("password_file"))
    except FileKeyringError as e:
        raise CredentialStoreError(str(e)) from e
    config.mkdir(parents=True, exist_ok=True)
    keyring.set_keyring(kr)
    _file_keyring_ready = True


@contextmanager
def _keychain() -> Iterator[None]:
    _use_file_keyring_if_requested()
    try:
        yield
    except NoKeyringError as e:
        raise CredentialStoreError(f"No system keychain is available. {_no_backend_advice()}") from e
    except KeyringError as e:
        raise CredentialStoreError(f"The system keychain refused the request: {e}") from e


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
            return json.loads(self._index.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, account: str, cred: Credential, meta: dict[str, str] | None = None) -> None:
        with _keychain():
            keyring.set_password(SERVICE, self._key(account), cred.model_dump_json())
        index = self._read_index()
        added_at = index.get(account, {}).get("added_at") or datetime.now(UTC).date().isoformat()
        index[account] = {**(meta or {}), "username": cred.username, "added_at": added_at}
        self._write_index(index)

    def _write_index(self, index: dict[str, dict[str, str]]) -> None:
        self._index.parent.mkdir(parents=True, exist_ok=True)
        self._index.write_text(json.dumps(index, indent=2), encoding="utf-8")

    def update_meta(self, account: str, **fields: str) -> bool:
        """Change non-secret index fields (e.g. company). False if the account isn't saved."""
        index = self._read_index()
        if account not in index:
            return False
        index[account].update(fields)
        self._write_index(index)
        return True

    def get(self, account: str) -> Credential | None:
        with _keychain():
            raw = keyring.get_password(SERVICE, self._key(account))
        return Credential.model_validate_json(raw) if raw else None

    def delete(self, account: str) -> bool:
        index = self._read_index()
        existed = index.pop(account, None) is not None
        with _keychain():
            try:
                keyring.delete_password(SERVICE, self._key(account))
            except PasswordDeleteError:  # nothing stored under that key
                pass
        self._write_index(index)
        return existed

    def list(self) -> dict[str, dict[str, str]]:
        """account -> non-secret metadata (always includes username). Never returns passwords."""
        return self._read_index()
