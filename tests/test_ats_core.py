import sys

import keyring
import pytest
from ats_core import Credential, CredentialStore, CredentialStoreError, credentials, snapshot
from keyring.backends import fail


def test_missing_keychain_gives_actionable_error(tmp_path, monkeypatch):
    keyring.set_keyring(fail.Keyring())
    store = CredentialStore("workday", tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(CredentialStoreError, match="Secret Service"):
        store.save("acme.wd1.myworkdayjobs.com", Credential(username="u", password="p"))
    with pytest.raises(CredentialStoreError, match="No system keychain"):
        store.get("acme.wd1.myworkdayjobs.com")
    assert store.list() == {}  # nothing half-written to the index


def test_delete_of_unknown_account_is_quiet(tmp_path):
    assert CredentialStore("workday", tmp_path).delete("nope.example.com") is False


def test_windows_dirs_use_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert credentials.default_config_dir() == tmp_path / "roaming" / "work-mcps"
    assert snapshot.default_cache_dir() == tmp_path / "local" / "work-mcps"


def test_unix_dirs_follow_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert credentials.default_config_dir() == tmp_path / "cfg" / "work-mcps"
    assert snapshot.default_cache_dir() == tmp_path / "cache" / "work-mcps"


def test_plaintext_keyring_roundtrip_and_mode(tmp_path, monkeypatch):
    import os
    import stat

    import keyring

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(credentials, "_file_keyring_ready", False)
    monkeypatch.setenv("WORK_MCP_KEYRING", "plaintext")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    store = CredentialStore("workday", tmp_path / "idx")
    store.save("h.example.com", Credential(username="u", password="secret-pw"))
    assert store.get("h.example.com").password == "secret-pw"  # type: ignore[union-attr]
    data_file = tmp_path / "work-mcps" / "credentials.json"
    assert "secret-pw" in data_file.read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(data_file.stat().st_mode) == 0o600
    assert store.delete("h.example.com")
    assert store.get("h.example.com") is None
    keyring.set_keyring(keyring.get_keyring())  # leave conftest's backend replaced by a fresh one


def test_plaintext_keyring_refused_off_linux(monkeypatch):

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(credentials, "_file_keyring_ready", False)
    monkeypatch.setenv("WORK_MCP_KEYRING", "plaintext")
    with pytest.raises(CredentialStoreError, match="only available on Linux"):
        CredentialStore("workday").get("x")
