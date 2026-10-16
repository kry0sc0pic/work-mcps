from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest
from ats_core import ApplicationCache, Credential
from mcp.server.mcpserver.exceptions import ToolError
from mcp_avature.accounts import AvatureAccount
from mcp_avature.auth import AuthError, load_cached_session
from mcp_avature.client import AvatureClient
from mcp_avature.config import AvatureSettings
from mcp_avature.parse import is_logged_in, is_login_page, parse_applications, parse_date
from mcp_avature.server import (
    avature_delete_credentials,
    avature_get_applications,
    avature_get_delta,
    avature_list_accounts,
    credential_store,
)
from mcp_avature.waf import WafError, WafPass, challenged

HOST = "acme.avature.net"
CRED = Credential(username="me@x.com", password="Pw-Case1")
AGENT = "UA-test"


def row(job: int, title: str, status: str, applied: str) -> str:
    return (
        f'<tr><td><a class="link" href="https://{HOST}/en_US/careers/JobDetail?jobId={job}">{title}</a></td>'
        f"<td>Country, State, City</td><td>{status}</td><td>{applied}</td></tr>"
    )


def profile(status: str = "Qualifications Under Review") -> str:
    rows = row(11, "Intern, Analytics &amp; Data", status, "08/20/2026") + row(
        12, "Co-op, Engineering", "Not selected for this role", "08/17/2026"
    )
    return (
        f'<a href="https://{HOST}/en_US/careers/Logout/">Logout</a><h3>Jobs I\'ve applied to</h3>'
        f"<table><thead><tr><th>Job Title</th></tr></thead><tbody>{rows}</tbody></table>"
    )


LOGIN_PAGE = (
    '<form method="post" action="/en_US/careers/Login"><input type="email" name="username">'
    '<input type="password" name="password"></form>'
)


class FakeSite:
    def __init__(self, password: str = CRED.password):
        self.password = password
        self.logins = 0
        self.status = "Qualifications Under Review"
        self.expire = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        jar = req.headers.get("cookie", "")
        if "aws-waf-token=tok" not in jar or req.headers["user-agent"] != AGENT and "aws-waf-token" in jar:
            return httpx.Response(
                202,
                text="<script src='x.awswaf.com/challenge.js'>",
                headers={"x-amzn-waf-action": "challenge"},
            )
        path = req.url.path
        if path == "/en_US/careers/Login" and req.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE)
        if path == "/en_US/careers/Login" and req.method == "POST":
            form = parse_qs(req.content.decode())
            if form["username"] != [CRED.username] or form["password"] != [self.password]:
                return httpx.Response(200, text=LOGIN_PAGE)
            self.logins += 1
            self.expire = False
            return httpx.Response(
                302, headers={"location": "/en_US/careers/Profile", "set-cookie": "sess=ok; Path=/"}
            )
        if path == "/en_US/careers/Profile" and "sess=ok" in jar and not self.expire:
            return httpx.Response(200, text=profile(self.status))
        return httpx.Response(200, text=LOGIN_PAGE)


class Tokens:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, url: str) -> WafPass:
        assert url == f"https://{HOST}/en_US/careers/Login"
        self.calls += 1
        return WafPass({"aws-waf-token": "tok"}, AGENT)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = AvatureSettings(cache_dir=tmp_path / "cache", config_dir=tmp_path / "config")
    monkeypatch.setattr("mcp_avature.server.AvatureSettings", lambda: s)
    return s


def client(site: FakeSite, settings: AvatureSettings, tokens: Tokens) -> AvatureClient:
    return AvatureClient(
        AvatureAccount(host=HOST), CRED, settings, transport=httpx.MockTransport(site), token_provider=tokens
    )


def test_account_from_url():
    a = AvatureAccount.from_url("https://Acme.avature.net/fr_CA/jobs/Login?x=1")
    assert (a.host, a.portal) == (HOST, "/fr_CA/jobs")
    assert AvatureAccount.from_url("acme.avature.net").portal == "/en_US/careers"
    assert AvatureAccount.from_url("acme.avature.net").default_company == "Acme"
    custom = AvatureAccount.from_url("https://apply.example.com/en_US/careers/Login")
    assert (custom.host, custom.portal, custom.default_company) == (
        "apply.example.com",
        "/en_US/careers",
        "Example",
    )
    for bad in ("https://example.com/", "https://avature.net.evil.com/x", ""):
        with pytest.raises(ValueError):
            AvatureAccount.from_url(bad)


def test_parse_applications_by_header():
    html = (
        "<table><thead><tr><th>Position</th><th>Application date</th><th>Status</th><th>Withdraw</th></tr>"
        "</thead>"
        '<tbody><tr><td><a href="https://apply.example.com/en_US/careers/JobDetail/Analyst-Intern/4242">'
        "Analyst &amp; Intern</a></td><td>10-15-2026</td><td>Applied and pending review</td>"
        '<td><a href="/en_US/careers/CancelApplicationCheck?jobId=4242">Withdraw</a></td></tr>'
        "</tbody></table>"
    )
    [a] = parse_applications(html)
    assert (a.job_id, a.title, a.location, a.status, a.applied_on) == (
        "4242",
        "Analyst & Intern",
        "",
        "Applied and pending review",
        date(2026, 10, 15),
    )


def test_parsers():
    apps = parse_applications(profile())
    assert [(a.job_id, a.title, a.status, a.applied_on) for a in apps] == [
        ("11", "Intern, Analytics & Data", "Qualifications Under Review", date(2026, 8, 20)),
        ("12", "Co-op, Engineering", "Not selected for this role", date(2026, 8, 17)),
    ]
    assert parse_applications("<p>nothing</p>") == []
    assert parse_date("08/20/2026") == date(2026, 8, 20) and parse_date("junk") is None
    assert is_logged_in(profile()) and not is_logged_in(LOGIN_PAGE)
    assert is_login_page(LOGIN_PAGE)
    waf = httpx.Response(202, text="x", headers={"x-amzn-waf-action": "challenge"})
    assert challenged(waf) and not challenged(httpx.Response(200, text="ok"))


async def test_applications_earn_a_token_log_in_and_cache_the_session(settings):
    site, tokens = FakeSite(), Tokens()
    async with client(site, settings, tokens) as c:
        apps = await c.applications()
    assert [(a.id, a.state, a.applied_on) for a in apps] == [
        ("11", "active", date(2026, 8, 20)),
        ("12", "inactive", date(2026, 8, 17)),
    ]
    assert site.logins == 1 and tokens.calls == 1
    saved = load_cached_session(settings.session_path(HOST))
    assert saved and saved.cookies["aws-waf-token"] == "tok" and saved.user_agent == AGENT
    # A second client reuses the stored session: no browser, no login.
    async with client(site, settings, tokens) as c:
        await c.applications()
    assert site.logins == 1 and tokens.calls == 1


async def test_expired_session_logs_in_again(settings):
    site, tokens = FakeSite(), Tokens()
    async with client(site, settings, tokens) as c:
        await c.applications()
    site.expire = True
    async with client(site, settings, tokens) as c:
        await c.applications()
    assert site.logins == 2 and tokens.calls == 1


async def test_wrong_password(settings):
    async with client(FakeSite(password="other"), settings, Tokens()) as c:
        with pytest.raises(AuthError):
            await c.applications()


async def test_token_that_keeps_failing_raises(settings):
    class Bad(Tokens):
        async def __call__(self, url: str) -> WafPass:
            return WafPass({"aws-waf-token": "nope"}, AGENT)

    async with client(FakeSite(), settings, Bad()) as c:
        with pytest.raises(WafError):
            await c.applications()


async def test_tools_end_to_end(settings, monkeypatch):
    site, tokens = FakeSite(), Tokens()
    credential_store(settings).save(
        HOST, CRED, meta={"company": "Acme", "source": "cli", "portal": "/en_US/careers"}
    )
    real = AvatureClient.__init__

    def init(self, account, cred, s, transport=None, token_provider=None):
        real(self, account, cred, s, transport=httpx.MockTransport(site), token_provider=tokens)

    monkeypatch.setattr(AvatureClient, "__init__", init)
    assert HOST in await avature_list_accounts()
    res = await avature_get_applications(company="acme", applied_after=date(2026, 8, 18))
    assert [a.id for a in res.applications] == ["11"] and res.applications[0].company == "Acme"
    assert not res.accounts[0].from_cache
    assert (await avature_get_applications()).accounts[0].from_cache
    assert (await avature_get_delta()).deltas[HOST].baseline
    site.status = "Interview"
    changed = (await avature_get_delta()).deltas[HOST].status_changed
    assert [c.application.id for c in changed] == ["11"]
    with pytest.raises(ToolError):
        await avature_get_applications(company="nobody")
    assert await avature_delete_credentials(HOST)
    assert ApplicationCache(settings.db_path).load([HOST]) == []
