"""Opt-in file-based keyrings for machines with no OS keychain (headless Linux servers).

Selected only by an explicit choice, never as a silent fallback: `work-mcp headless` writes
`keyring.json` in the config dir (or set `WORK_MCP_KEYRING=file|plaintext`). Two modes:
- `file`: Argon2 + AES encrypted `credentials.enc`; the master password comes from a password file
  (`WORK_MCP_KEYRING_PASSWORD_FILE` / the saved path) or `WORK_MCP_KEYRING_PASSWORD`.
- `plaintext` (Linux only): JSON `credentials.json`, mode 600, readable by anyone who can read the file.
"""

import json
import os
import sys
from pathlib import Path

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class FileKeyringError(RuntimeError):
    """The file keyring is selected but can't be used. The message says what to fix."""


def read_password(password_file: str | None) -> str | None:
    path = os.environ.get("WORK_MCP_KEYRING_PASSWORD_FILE") or password_file
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip() or None
        except OSError as e:
            raise FileKeyringError(f"Can't read the keyring password file ({path}): {e}") from e
    return os.environ.get("WORK_MCP_KEYRING_PASSWORD", "").strip() or None


def make_encrypted_keyring(path: Path, password_file: str | None) -> KeyringBackend:
    try:
        from keyrings.cryptfile.cryptfile import CryptFileKeyring  # pyright: ignore[reportMissingImports]
    except ImportError as e:
        raise FileKeyringError(
            "The encrypted-file keyring needs keyrings.cryptfile (installed by default on Linux; "
            "run `uv sync --all-packages`)."
        ) from e

    password = read_password(password_file)
    if password is None:
        raise FileKeyringError(
            "The encrypted-file keyring needs its master password: run `work-mcp headless`, or set "
            "WORK_MCP_KEYRING_PASSWORD_FILE to a file containing it."
        )

    class FileKeyring(CryptFileKeyring):
        def set_password(self, service, username, password):
            super().set_password(service, username, password)
            if sys.platform != "win32":
                os.chmod(self.file_path, 0o600)

    kr = FileKeyring()
    kr.file_path = str(path)  # type: ignore[assignment]  # overridable at instance level per the base class
    kr.keyring_key = password
    return kr


class PlaintextKeyring(KeyringBackend):
    """Passwords as JSON in one file readable only by the owner. No encryption: use only when there is
    no OS keychain and you accept that anyone who can read the file can read the logins."""

    priority = 1  # type: ignore[assignment]

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def get_password(self, service, username):
        return self._read().get(service, {}).get(username)

    def set_password(self, service, username, password):
        data = self._read()
        data.setdefault(service, {})[username] = password
        self._write(data)

    def delete_password(self, service, username):
        data = self._read()
        if username not in data.get(service, {}):
            raise PasswordDeleteError("not found")
        del data[service][username]
        self._write(data)
