import json
import sys

import pytest
from typer.testing import CliRunner
from work_mcp import cli
from work_mcp.clients import ServerSpec, all_clients
from work_mcp.importer import CsvFormatError, build_plan
from work_mcp.platforms import PLATFORMS

runner = CliRunner()
WD = PLATFORMS["workday"]

CHROME = """name,url,username,password,note
hooli,https://hooli.wd5.myworkdayjobs.com/en-US/HooliExternalCareerSite?source=li,me@x.com,pw1,
github,https://github.com/login,me@x.com,ghpw,
initech,initech.wd3.myworkdayjobs.com/en-US/jobs-and-careers,me@x.com,pw2,
hostonly,https://acme.wd1.myworkdayjobs.com,me@x.com,pw3,
nopass,https://beta.wd1.myworkdayjobs.com/Careers,me@x.com,,
dup,https://hooli.wd5.myworkdayjobs.com/en-US/HooliExternalCareerSite,me@x.com,pw1,
conflict,https://hooli.wd5.myworkdayjobs.com/en-US/HooliExternalCareerSite,other@x.com,pw9,
"""
APPLE = """Title,URL,Username,Password,Notes,OTPAuth
Umbrella,https://umbrella.wd5.myworkdayjobs.com/en-US/REC_Umbrella_Ext_Gateway/login,me@x.com,upw,,
"""
BITWARDEN = """folder,type,name,login_uri,login_username,login_password
,login,x,https://acme.wd3.myworkdayjobs.com/Careers,me@x.com,bwpw
"""


@pytest.fixture(autouse=True)
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKDAY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("WORKDAY_CACHE_DIR", str(tmp_path / "cache"))


def stored_password(key):
    cred = WD.store().get(key)
    assert cred
    return cred.password


def write(tmp_path, text, name="export.csv"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_plan_classifies_rows(tmp_path):
    plan = build_plan(write(tmp_path, CHROME))
    statuses = sorted(i.status for i in plan.items)
    # hooli, initech = new; host-only acme = lookup; nopass = skipped; conflicting hooli login = conflict
    assert statuses == ["conflict", "lookup", "new", "new", "skipped"]  # exact duplicate row collapsed
    assert plan.rows_total == 7 and plan.rows_ignored == 1  # github
    skipped = {i.source_url: i.detail for i in plan.items if i.status == "skipped"}
    assert "no username or password" in skipped["https://beta.wd1.myworkdayjobs.com/Careers"]


def test_host_only_rows_count_as_workday_without_a_site(tmp_path):
    csv_text = "Title,URL,Username,Password\nGlobex,https://globex.wd1.myworkdayjobs.com,me@x.com,pw\n"
    plan = build_plan(write(tmp_path, csv_text))
    (item,) = plan.writable()
    assert item.status == "lookup" and item.platform.id == "workday" and item.ref is None
    assert item.company_hint == "Globex"


ROBOTS = """User-agent: *
Sitemap: https://globex.wd1.myworkdayjobs.com/Globex_Career/siteMap.xml
Sitemap: https://globex.wd1.myworkdayjobs.com/Other_Site/siteMap.xml
Sitemap: https://other.wd1.myworkdayjobs.com/Nope/siteMap.xml
Sitemap: https://globex.wd1.myworkdayjobs.com/Globex_Career/siteMap.xml
"""


def test_sites_from_robots():
    from work_mcp.platforms import sites_from_robots

    assert sites_from_robots(ROBOTS, "globex.wd1.myworkdayjobs.com") == ["Globex_Career", "Other_Site"]
    assert sites_from_robots("User-agent: *\nDisallow: /", "globex.wd1.myworkdayjobs.com") == []


@pytest.mark.parametrize(
    "text, host", [(APPLE, "umbrella.wd5.myworkdayjobs.com"), (BITWARDEN, "acme.wd3.myworkdayjobs.com")]
)
def test_other_export_formats(tmp_path, text, host):
    (item,) = build_plan(write(tmp_path, text)).items
    assert item.status == "new" and item.ref and item.ref.key == host


def test_company_hint_from_title_column(tmp_path):
    plan = build_plan(write(tmp_path, CHROME))
    companies = {i.ref.key: i.ref.meta["company"] for i in plan.items if i.ref and i.status == "new"}
    assert companies["hooli.wd5.myworkdayjobs.com"] == "hooli"  # from the "name" column
    assert companies["initech.wd3.myworkdayjobs.com"] == "initech"
    (item,) = build_plan(write(tmp_path, BITWARDEN, "bw.csv")).items
    assert item.ref and item.ref.meta["company"] == "x"  # Bitwarden name column
    assert item.ref.meta["source"] == "csv-import"


def test_unknown_csv_layout(tmp_path):
    with pytest.raises(CsvFormatError):
        build_plan(write(tmp_path, "a,b\n1,2\n"))


def test_import_cli_dry_run_then_apply(tmp_path):
    f = write(tmp_path, APPLE)
    r = runner.invoke(cli.app, ["import", str(f), "--dry-run"])
    assert r.exit_code == 0 and "upw" not in r.output and "new" in r.output
    assert WD.store().list() == {}
    assert f.exists()

    r = runner.invoke(cli.app, ["import", str(f), "--yes", "--delete"])
    assert r.exit_code == 0 and "upw" not in r.output
    assert WD.store().list()["umbrella.wd5.myworkdayjobs.com"]["site"] == "REC_Umbrella_Ext_Gateway"
    assert stored_password("umbrella.wd5.myworkdayjobs.com") == "upw"
    assert not f.exists()


def test_reimport_is_unchanged_then_update(tmp_path):
    f = write(tmp_path, APPLE)
    runner.invoke(cli.app, ["import", str(f), "--yes", "--keep"])
    (item,) = build_plan(f).items
    assert item.status == "unchanged"
    f.write_text(APPLE.replace("upw", "newpw"))
    (item,) = build_plan(f).items
    assert item.status == "update"


def test_setup_wizard_manual_add(monkeypatch):
    monkeypatch.setattr(cli, "read_password", lambda prompt: "typed-pw")
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    answers = "\n2\nhttps://hooli.wd5.myworkdayjobs.com/en-US/HooliExternalCareerSite\nme@x.com\n\n"
    r = runner.invoke(cli.app, ["setup"], input=answers)
    assert r.exit_code == 0, r.output
    assert "typed-pw" not in r.output
    assert stored_password("hooli.wd5.myworkdayjobs.com") == "typed-pw"
    assert "claude mcp add --scope user workday" in r.output  # nothing detected -> prints commands


def test_setup_wizard_csv_import(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    f = write(tmp_path, APPLE)
    r = runner.invoke(cli.app, ["setup"], input=f"\n1\n{f}\ny\ny\n")
    assert r.exit_code == 0, r.output
    assert "upw" not in r.output
    assert "umbrella.wd5.myworkdayjobs.com" in WD.store().list()
    assert not f.exists()


def test_register_command_shape():
    spec = cli.server_spec(WD)
    cmd = all_clients()["claude"].argv(spec)
    assert cmd[:6] == ["claude", "mcp", "add", "--scope", "user", "workday"]
    assert cmd[-1] == "mcp-workday" and "--project" in cmd


@pytest.mark.parametrize("client", ["codex", "grok"])
def test_codex_and_grok_use_mcp_add(client):
    cmd = all_clients()[client].argv(ServerSpec("workday", "uv", ["run", "x"]))
    assert cmd == [client, "mcp", "add", "workday", "--", "uv", "run", "x"]


def test_openclaw_takes_json():
    cmd = all_clients()["openclaw"].argv(ServerSpec("workday", "uv", ["run", "x"]))
    assert cmd[:4] == ["openclaw", "mcp", "set", "workday"]
    assert json.loads(cmd[4]) == {"command": "uv", "args": ["run", "x"]}


def test_cursor_json_merge_keeps_other_servers(fake_home):
    cfg = fake_home / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}), encoding="utf-8")
    cursor, spec = all_clients()["cursor"], ServerSpec("workday", "uv", ["run", "x"])
    assert cursor.install(spec).status == "added"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["keep"] == 1 and "other" in data["mcpServers"]
    assert data["mcpServers"]["workday"] == {"command": "uv", "args": ["run", "x"]}
    assert cursor.install(spec).status == "exists"


def test_cursor_creates_config_and_rejects_garbage(fake_home):
    cursor, spec = all_clients()["cursor"], ServerSpec("workday", "uv", [])
    assert cursor.install(spec).status == "added"
    (fake_home / ".cursor" / "mcp.json").write_text("[1]", encoding="utf-8")
    assert cursor.install(spec).status == "failed"


def test_hermes_yaml_keeps_comments(fake_home):
    cfg = fake_home / ".hermes" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        "# my settings\nmodel: x\nmcp_servers:\n  other:\n    command: y  # keep\n", encoding="utf-8"
    )
    hermes, spec = all_clients()["hermes"], ServerSpec("workday", "uv", ["run", "x"])
    assert hermes.install(spec).status == "added"
    text = cfg.read_text(encoding="utf-8")
    assert "# my settings" in text and "# keep" in text and "other:" in text
    assert "workday:" in text and hermes.install(spec).status == "exists"


def test_detection(fake_home, monkeypatch):
    monkeypatch.setattr("work_mcp.clients.shutil.which", lambda _: None)
    assert not any(c.detected() for c in all_clients().values())
    (fake_home / ".cursor").mkdir()
    assert [c.id for c in all_clients().values() if c.detected()] == ["cursor"]


def test_missing_cli_prints_manual_command(monkeypatch):
    monkeypatch.setattr("work_mcp.clients.shutil.which", lambda _: None)
    out = all_clients()["codex"].install(ServerSpec("workday", "uv", []))
    assert out.status == "failed" and "not found" in out.message


def test_register_command_with_client_flag(fake_home):
    r = runner.invoke(cli.app, ["register", "-c", "cursor"])
    assert r.exit_code == 0, r.output
    assert (
        "workday"
        in json.loads((fake_home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    )
    assert runner.invoke(cli.app, ["register", "-c", "nope"]).exit_code != 0


def test_bad_platform():
    r = runner.invoke(cli.app, ["import", "x.csv", "-p", "nope"])
    assert r.exit_code != 0


def _pick(plan, keys: str, monkeypatch) -> list[str]:
    """Drive the real questionary checkbox with simulated key presses."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", Tty())
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        return cli.choose_platforms(plan)


def test_platform_picker_defaults_to_all_selected(tmp_path, monkeypatch):
    plan = build_plan(write(tmp_path, APPLE))
    assert _pick(plan, "\r", monkeypatch) == ["workday"]


def test_platform_picker_can_deselect(tmp_path, monkeypatch):
    plan = build_plan(write(tmp_path, APPLE))
    assert _pick(plan, " \r", monkeypatch) == []  # space toggles the first row off


def test_picker_skipped_when_nothing_importable(tmp_path):
    plan = build_plan(write(tmp_path, "Title,URL,Username,Password\nGH,https://github.com,me,pw\n"))
    assert cli.choose_platforms(plan) == []


def test_deselected_platform_is_not_saved(tmp_path, monkeypatch):
    f = write(tmp_path, APPLE)
    monkeypatch.setattr(cli, "choose_platforms", lambda plan: [])
    r = runner.invoke(cli.app, ["import", str(f), "--keep"])
    assert r.exit_code == 0 and "Nothing imported" in r.output
    assert WD.store().list() == {}
    assert f.exists()


def test_missing_file_is_a_clean_error(tmp_path):
    for args in (["--dry-run"], []):
        r = runner.invoke(cli.app, ["import", str(tmp_path / "nope.csv"), *args])
        assert r.exit_code == 1 and "Can't read" in r.output and "Traceback" not in r.output


@pytest.mark.parametrize(
    "platform, raw, expected",
    [
        ("darwin", "'/Users/me/My\\ Files/pw.csv' ", "/Users/me/My Files/pw.csv"),
        ("linux", "/home/me/My\\ Files/pw.csv", "/home/me/My Files/pw.csv"),
        ("win32", '"C:\\Users\\me\\Downloads\\pw.csv"', "C:\\Users\\me\\Downloads\\pw.csv"),
    ],
)
def test_clean_path(monkeypatch, platform, raw, expected):
    monkeypatch.setattr(sys, "platform", platform)
    assert str(cli.clean_path(raw)).replace("\\", "/") == expected.replace("\\", "/")


def test_hermes_gets_dbus_env_on_linux(fake_home, monkeypatch):
    monkeypatch.setattr("work_mcp.clients.sys.platform", "linux")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    all_clients()["hermes"].install(ServerSpec("workday", "uv", []))
    text = (fake_home / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert "DBUS_SESSION_BUS_ADDRESS" in text and "/run/user/1000/bus" in text


def test_headless_command_plaintext_and_encrypted(fake_home, monkeypatch):
    from ats_core.credentials import keyring_settings_path

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / "cfg"))
    r = runner.invoke(cli.app, ["headless", "--mode", "plaintext", "--yes"])
    assert r.exit_code == 0, r.output
    assert json.loads(keyring_settings_path().read_text(encoding="utf-8")) == {"backend": "plaintext"}
    r = runner.invoke(cli.app, ["headless", "--mode", "encrypted"])
    cfg = json.loads(keyring_settings_path().read_text(encoding="utf-8"))
    assert cfg["backend"] == "file" and (fake_home / "cfg" / "work-mcps" / "keyring.key").exists()
    assert runner.invoke(cli.app, ["headless", "--off"]).exit_code == 0
    assert not keyring_settings_path().exists()


def test_headless_refused_off_linux(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert runner.invoke(cli.app, ["headless", "--mode", "plaintext", "--yes"]).exit_code == 1


def test_add_oracle_command_saves_passwordless_account(monkeypatch):
    from mcp_oracle.accounts import OracleAccount

    acct = OracleAccount(host="careers.acme.com", site="CX_1", api_host="eg.fa.us2.oraclecloud.com")
    monkeypatch.setattr(
        "work_mcp.platforms.discover_api_host", lambda url: (OracleAccount.host_and_site(url), acct)[1]
    )
    url = "https://careers.acme.com/en/sites/CX_1/my-profile/sign-in"
    r = runner.invoke(cli.app, ["add-oracle", url, "me@x.com", "--company", "Acme Corp"])
    assert r.exit_code == 0, r.output
    assert "work-mcp mailbox" in r.output  # no mailbox configured in tests
    saved = PLATFORMS["oracle"].store().list()["careers.acme.com"]
    assert saved["username"] == "me@x.com" and saved["company"] == "Acme Corp" and saved["site"] == "CX_1"
    assert PLATFORMS["oracle"].store().get("careers.acme.com") is None  # nothing in the keychain

    bad = runner.invoke(cli.app, ["add-oracle", "https://careers.acme.com/jobs", "me@x.com"])
    assert bad.exit_code == 1 and "/sites/" in bad.output


def test_add_greenhouse_command_saves_passwordless_account():
    r = runner.invoke(cli.app, ["add-greenhouse", "me@x.com"])
    assert r.exit_code == 0, r.output
    store = PLATFORMS["greenhouse"].store()
    saved = store.list()["my.greenhouse.io"]
    assert saved["username"] == "me@x.com" and saved["mail_template"] == "greenhouse"
    assert store.get("my.greenhouse.io") is None  # nothing in the keychain
    assert PLATFORMS["greenhouse"].owns("https://my.greenhouse.io/users/sign_in")
    assert not PLATFORMS["greenhouse"].owns("https://boards.greenhouse.io/acme")


def test_add_waas_command_saves_passwordless_account():
    r = runner.invoke(cli.app, ["add-waas", "me@x.com"])
    assert r.exit_code == 0, r.output
    store = PLATFORMS["waas"].store()
    saved = store.list()["workatastartup.com"]
    assert saved["username"] == "me@x.com" and saved["mail_template"] == "ycombinator"
    assert store.get("workatastartup.com") is None  # nothing in the keychain
    assert PLATFORMS["waas"].owns("https://www.workatastartup.com/companies?role=eng")
    assert not PLATFORMS["waas"].owns("https://ycombinator.com/jobs")


def test_add_tal_command_saves_password_login(monkeypatch):
    monkeypatch.setattr(cli, "read_password", lambda prompt: "Pw-1")
    r = runner.invoke(
        cli.app, ["add-tal", "https://acme.tal.net/candidate", "--email", "me@x.com", "--company", "Acme"]
    )
    assert r.exit_code == 0, r.output
    store = PLATFORMS["tal"].store()
    assert store.list()["acme.tal.net"]["company"] == "Acme"
    cred = store.get("acme.tal.net")
    assert cred and (cred.username, cred.password) == ("me@x.com", "Pw-1")
    assert PLATFORMS["tal"].owns("https://acme.tal.net/vx/lang-en-GB/candidate")
    assert not PLATFORMS["tal"].owns("https://tal.net.evil.com/x")
    assert runner.invoke(cli.app, ["add-tal", "https://example.com/x", "--email", "a@b.c"]).exit_code == 1
