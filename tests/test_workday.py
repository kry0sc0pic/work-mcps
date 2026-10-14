import json
from datetime import UTC, date, datetime, timedelta

import pytest
from ats_core import ApplicationCache, Credential, CredentialStore, SnapshotStore, diff, is_fresh
from mcp_workday.accounts import WorkdayAccount
from mcp_workday.client import parse_date, to_application
from mcp_workday.config import WorkdaySettings
from mcp_workday.server import (
    filter_applications,
    load_account,
    select_accounts,
    workday_add_account,
    workday_delete_credentials,
    workday_list_accounts,
    workday_save_credentials,
    workday_set_company,
)

URL = "https://umbrella.wd5.myworkdayjobs.com/en-US/REC_Umbrella_Ext_Gateway/login?redirect=%2Fx"
ACCT = WorkdayAccount.from_url(URL)
CRED = Credential(username="me@example.com", password="pw")


def raw(id_: str, status: str, applied: str = "10 October 2026", title: str = "SWE Intern") -> dict:
    return {
        "applicationId": id_,
        "jobRequisitionId": "1",
        "postingTitle": title,
        "status": status,
        "dateApplied": applied,
        "canWithdraw": status == "Application Submitted",
    }


@pytest.fixture
def settings(tmp_path):
    return WorkdaySettings(cache_dir=tmp_path / "cache", config_dir=tmp_path / "config")


def test_account_from_url():
    assert (ACCT.host, ACCT.tenant, ACCT.site) == (
        "umbrella.wd5.myworkdayjobs.com",
        "umbrella",
        "REC_Umbrella_Ext_Gateway",
    )
    assert WorkdayAccount.from_url("https://acme.wd1.myworkdayjobs.com/Careers").site == "Careers"
    with pytest.raises(ValueError):
        WorkdayAccount.from_url("https://example.com/jobs")


@pytest.mark.parametrize(
    "url, host, tenant, site",
    [
        (
            "https://hooli.wd5.myworkdayjobs.com/en-US/HooliExternalCareerSite?source=jobboardlinkedin",
            "hooli.wd5.myworkdayjobs.com",
            "hooli",
            "HooliExternalCareerSite",
        ),
        (
            "hooli.wd5.myworkdayjobs.com/HooliExternalCareerSite/",
            "hooli.wd5.myworkdayjobs.com",
            "hooli",
            "HooliExternalCareerSite",
        ),
        (
            "  HTTPS://Hooli.WD5.MyWorkdayJobs.com/en-US/HooliExternalCareerSite/job/US-CA/Eng_JR1#x ",
            "hooli.wd5.myworkdayjobs.com",
            "hooli",
            "HooliExternalCareerSite",
        ),
        (
            "<https://acme.wd1.myworkdayjobs.com/fr-CA/Careers/login?redirect=%2Ffr-CA%2FCareers%2FuserHome>",
            "acme.wd1.myworkdayjobs.com",
            "acme",
            "Careers",
        ),
        (
            "https://acme.wd3.myworkdayjobs.com/en/Careers/apply",
            "acme.wd3.myworkdayjobs.com",
            "acme",
            "Careers",
        ),
        (
            "https://acme.wd5-impl.myworkdayjobs.com/Careers",
            "acme.wd5-impl.myworkdayjobs.com",
            "acme",
            "Careers",
        ),
        ("https://acme.wd1.myworkdayjobs.com:443/HR/job/x", "acme.wd1.myworkdayjobs.com", "acme", "HR"),
        (
            "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Ext_Site/jobs",
            "acme.wd1.myworkdayjobs.com",
            "acme",
            "Ext_Site",
        ),
    ],
)
def test_account_from_url_variants(url, host, tenant, site):
    a = WorkdayAccount.from_url(url)
    assert (a.host, a.tenant, a.site) == (host, tenant, site)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com/en-US/Careers",
        "https://myworkdayjobs.com.evil.com/Careers",
        "https://acme.wd1.myworkdayjobs.com",
        "https://acme.wd1.myworkdayjobs.com/en-US/",
    ],
)
def test_account_from_url_rejects(url):
    with pytest.raises(ValueError):
        WorkdayAccount.from_url(url)


def test_parse_date_formats():
    assert parse_date("28 August 2026") == parse_date("August 28, 2026") == date(2026, 8, 28)
    assert parse_date("garbage") is None


def apps():
    return [
        to_application(raw("a", "Application Submitted", title="Co-op"), "active", ACCT.key),
        to_application(raw("c", "Interview", "1 October 2026"), "active", ACCT.key),
        to_application(raw("b", "Not Selected", "August 19, 2026"), "inactive", ACCT.key),
    ]


def test_to_application_maps_fields():
    a = to_application(raw("b", "Not Selected", "August 19, 2026"), "inactive", ACCT.key)
    assert (a.id, a.account, a.state, a.applied_on) == ("b", ACCT.host, "inactive", date(2026, 8, 19))


def test_filters():
    assert [a.id for a in filter_applications(apps(), state="inactive")] == ["b"]
    assert [a.id for a in filter_applications(apps(), status="interview")] == ["c"]
    assert [a.id for a in filter_applications(apps(), title="co-op")] == ["a"]
    assert [a.id for a in filter_applications(apps(), applied_after=date(2026, 9, 1))] == ["a", "c"]


def test_delta_lifecycle(settings):
    store = SnapshotStore(settings.snapshot_path(ACCT.key))
    v1 = [
        to_application(raw("a", "Application Submitted"), "active", ACCT.key),
        to_application(raw("x", "Application Submitted"), "active", ACCT.key),
    ]
    d = diff(*store.load(), v1)
    assert d.baseline and len(d.new) == 2
    store.save(v1)

    v2 = [
        to_application(raw("n", "Application Submitted"), "active", ACCT.key),
        to_application(raw("a", "Not Selected"), "inactive", ACCT.key),
    ]
    d = diff(*store.load(), v2)
    assert not d.baseline
    assert [a.id for a in d.new] == ["n"]
    changes = [(c.application.id, c.previous_status) for c in d.status_changed]
    assert changes == [("a", "Application Submitted")]
    assert [a.id for a in d.removed] == ["x"]
    assert d.unchanged_count == 0


async def test_set_company_and_added_at_preserved(settings, monkeypatch):
    monkeypatch.setenv("WORKDAY_CONFIG_DIR", str(settings.config_dir))
    await workday_save_credentials(URL, "me@example.com", "pw", company="Umbrella")
    assert (await workday_list_accounts())[ACCT.host]["company"] == "Umbrella"
    index = settings.config_dir / "workday-accounts.json"
    data = json.loads(index.read_text())
    data[ACCT.host]["added_at"] = "2020-01-01"
    index.write_text(json.dumps(data))
    assert await workday_set_company(ACCT.host, " Raytheon ") is True
    assert (await workday_list_accounts())[ACCT.host]["company"] == "Raytheon"
    await workday_save_credentials(URL, "me@example.com", "pw2")  # re-save keeps the original date
    assert (await workday_list_accounts())[ACCT.host]["added_at"] == "2020-01-01"
    assert await workday_set_company("nope.example.com", "X") is False


async def test_credential_tools(settings, monkeypatch):
    monkeypatch.setenv("WORKDAY_CONFIG_DIR", str(settings.config_dir))
    monkeypatch.setenv("WORKDAY_CACHE_DIR", str(settings.cache_dir))

    async def fake_prompt(label, username):
        assert label == ACCT.host and username == "me@example.com"
        return CRED

    monkeypatch.setattr("mcp_workday.server.prompt_credentials", fake_prompt)
    saved = await workday_add_account(URL, "me@example.com")
    assert "pw" not in str(saved)
    await workday_delete_credentials(ACCT.host)
    saved = await workday_save_credentials(URL, "me@example.com", "pw")
    assert saved["account"] == ACCT.host and "pw" not in str(saved)
    assert saved["account"] == ACCT.host
    listing = await workday_list_accounts()
    entry = listing[ACCT.key]
    assert entry["site"] == ACCT.site and entry["username"] == "me@example.com"
    assert entry["company"] == "Umbrella" and entry["source"] == "typed" and entry["added_at"]
    assert "pw" not in str(listing)

    assert select_accounts(settings) == [ACCT.host]  # no filter -> every registered account
    acct, cred = load_account(settings, ACCT.host)
    assert acct == ACCT and cred.password == "pw"
    with pytest.raises(ValueError, match="Unknown account"):
        select_accounts(settings, "nope.example.com")

    assert await workday_delete_credentials(ACCT.key) is True
    with pytest.raises(ValueError, match="No Workday accounts"):
        select_accounts(settings)


def test_select_accounts_by_company(settings):
    store = CredentialStore("workday", settings.config_dir)
    for host, site, company in [
        ("hooli.wd5.myworkdayjobs.com", "NV", "Hooli"),
        ("umbrella.wd5.myworkdayjobs.com", "Umbrella", "Umbrella"),
        ("initech.wd3.myworkdayjobs.com", "jobs", "Initech"),
    ]:
        store.save(host, CRED, meta={"site": site, "company": company})
    assert select_accounts(settings) == sorted(store.list())
    assert select_accounts(settings, company="hool") == ["hooli.wd5.myworkdayjobs.com"]
    assert select_accounts(settings, company="UMBRELLA") == ["umbrella.wd5.myworkdayjobs.com"]  # by host
    assert select_accounts(settings, company="umbrella") == ["umbrella.wd5.myworkdayjobs.com"]
    assert select_accounts(settings, "initech.wd3.myworkdayjobs.com", company="hooli") == [
        "initech.wd3.myworkdayjobs.com"
    ]  # account wins over company
    with pytest.raises(ValueError, match="Registered: Hooli, Initech, Umbrella"):
        select_accounts(settings, company="google")


def test_cache_replace_history_and_removal(settings):
    cache = ApplicationCache(settings.db_path)
    t0, t1, t2 = (datetime(2026, 10, d, tzinfo=UTC) for d in (1, 2, 3))
    a = to_application(raw("a", "Application Submitted"), "active", "acme")
    x = to_application(raw("x", "Application Submitted"), "active", "acme")
    other = to_application(raw("o", "Application Submitted"), "active", "other")
    cache.replace("acme", [a, x], now=t0)
    cache.replace("other", [other], now=t0)
    assert {p.id for p in cache.load(["acme"])} == {"a", "x"}
    assert {p.id for p in cache.load()} == {"a", "x", "o"}
    assert cache.synced_at("acme") == t0

    a2 = to_application(raw("a", "Not Selected"), "inactive", "acme")
    cache.replace("acme", [a2], now=t1)  # a changed status, x disappeared
    loaded = cache.load(["acme"])
    assert [(p.id, p.status, p.state) for p in loaded] == [("a", "Not Selected", "inactive")]
    cache.replace("acme", [a2], now=t2)  # unchanged: no new history rows

    events = cache.history(["acme"], "a")
    assert [(e.status, e.seen_at[:10]) for e in events] == [
        ("Not Selected", "2026-10-02"),
        ("Application Submitted", "2026-10-01"),
    ]
    assert len(cache.history()) == 4  # a x2, x, o
    assert cache.history(["acme"], limit=1)[0].status == "Not Selected"

    cache.clear("acme")
    assert cache.load(["acme"]) == [] and cache.synced_at("acme") is None
    assert len(cache.load(["other"])) == 1


def test_cache_keeps_first_seen_and_dates(settings):
    cache = ApplicationCache(settings.db_path)
    a = to_application(raw("a", "Application Submitted", "August 19, 2026"), "active", "acme")
    cache.replace("acme", [a], now=datetime(2026, 10, 1, tzinfo=UTC))
    cache.replace("acme", [a], now=datetime(2026, 10, 9, tzinfo=UTC))
    (loaded,) = cache.load()
    assert loaded.applied_on == date(2026, 8, 19) and loaded.can_withdraw is True
    import sqlite3

    row = (
        sqlite3.connect(settings.db_path).execute("select first_seen, last_seen from applications").fetchone()
    )
    assert row == ("2026-10-01T00:00:00+00:00", "2026-10-09T00:00:00+00:00")


def test_is_fresh():
    now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    ttl = timedelta(minutes=30)
    assert is_fresh(now - timedelta(minutes=29), ttl, now)
    assert not is_fresh(now - timedelta(minutes=31), ttl, now)
    assert not is_fresh(None, ttl, now)


def test_dialog_passes_values_as_argv(monkeypatch):
    from mcp_workday import prompt

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "secret\n"})()

    monkeypatch.setattr(prompt.subprocess, "run", fake_run)
    assert prompt._dialog('evil" & quit', hidden=True) == "secret"
    cmd = calls[0]
    assert cmd[-3:] == ['evil" & quit', "", "hidden"]  # message is an argument, not script source
    assert not any("evil" in c for c in cmd[:-3])


def test_dialog_cancel(monkeypatch):
    from mcp_workday import prompt

    monkeypatch.setattr(
        prompt.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})()
    )
    with pytest.raises(prompt.PromptCancelledError):
        prompt._dialog("x")
