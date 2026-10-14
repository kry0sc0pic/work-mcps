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
