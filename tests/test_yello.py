from urllib.parse import parse_qs

import httpx
import pytest
from ats_core import Credential
from mcp_yello.accounts import YelloAccount
from mcp_yello.auth import AuthError, load_cached_session
from mcp_yello.client import YelloClient
from mcp_yello.config import YelloSettings
from mcp_yello.parse import hidden_form, is_logged_in, is_login_page, parse_applications, parse_schedule

HOST = "acme.yello.co"
SSO = "uk-p-sso.yello.co"
CRED = Credential(username="me@x.com", password="Pw-Case1")


def card(app_id: int, req: int, title: str, status: str, withdraw: bool = False) -> str:
    button = '<div class="candidatePortal__actions"><button>Withdraw</button></div>' if withdraw else ""
    return (
        '<div class="candidatePortal__eventCard" data-id="application">'
        f'<input type="hidden" name="{app_id}" value="{app_id}" data-id="application-id" />'
        f'<input type="hidden" name="{req}" value="{req}" data-id="requisition-id" />'
        '<div class="candidatePortal__eventCardHeader"><div class="candidatePortal__eventText">'
        f'<div class="yello-label">{title}</div><div class="yello-text">{status}</div></div>{button}'
        "</div></div>"
    )


def home(status: str = "Application Submitted", interviews: str = "") -> str:
    empty = '<div class="candidatePortal__row b_a_0">You currently have no {} scheduled.</div>'
    return (
        '<main><div class="candidatePortal__container candidateFacing__aside">'
        '<h2 class="candidatePortal__heading">Invitations</h2>'
        '<div class="candidatePortal__row b_a_0"><p>You currently have no outstanding invites.</p></div>'
        "</div>"
        '<div class="candidatePortal__container"><h2 class="candidatePortal__heading">My Schedule</h2>'
        + empty.format("events")
        + '</div><div class="candidatePortal__container">'
        '<h2 class="candidatePortal__heading">My Interviews</h2>'
        + (interviews or empty.format("interviews"))
        + '</div><div class="candidatePortal__container">'
        '<h2 class="candidatePortal__heading">My Applications</h2>'
        + card(11, 901, "Analyst &amp; Data - Intern - Summer 2027", status, withdraw=True)
        + card(12, 902, "Engineering - Intern", "Not selected")
        + "</div></main>"
    )


CAS = (
    '<form id="fm1" method="post" action="login"><input type="text" name="username" />'
    '<input type="password" name="password" /><input type="hidden" name="execution" value="e1s1" />'
    '<input type="hidden" name="_eventId" value="submit" /><input type="hidden" name="geolocation" /></form>'
)


class FakeSite:
    def __init__(self, password: str = CRED.password):
        self.password = password
        self.logins = 0
        self.expire = False
        self.status = "Application Submitted"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        jar = req.headers.get("cookie", "")
        host, path = req.url.host, req.url.path
        if host == HOST and path == "/login" and "ticket" not in req.url.params:
            return httpx.Response(302, headers={"location": f"https://{SSO}/cas/login?service=x"})
        if host == SSO and req.method == "GET":
            return httpx.Response(200, text=CAS)
        if host == SSO and req.method == "POST":
            if "service" not in req.url.params:  # the real CAS server rejects a post without it
                return httpx.Response(401, text=CAS)
            form = parse_qs(req.content.decode())
            if (
                form["username"] != [CRED.username]
                or form["password"] != [self.password]
                or form["execution"] != ["e1s1"]
            ):
                return httpx.Response(200, text=CAS)
            self.logins += 1
            self.expire = False
            return httpx.Response(302, headers={"location": f"https://{HOST}/login?ticket=ST-1"})
        if host == HOST and path == "/login":
            return httpx.Response(302, headers={"location": "/", "set-cookie": "_session_id=ok; Path=/"})
        if host == HOST and path == "/" and "_session_id=ok" in jar and not self.expire:
            return httpx.Response(200, text=home(self.status))
        return httpx.Response(302, headers={"location": f"https://{SSO}/cas/login?service=x"})


@pytest.fixture
def settings(tmp_path):
    return YelloSettings(cache_dir=tmp_path / "cache", config_dir=tmp_path / "config")


def client(site: FakeSite, settings: YelloSettings) -> YelloClient:
    return YelloClient(YelloAccount(host=HOST), CRED, settings, transport=httpx.MockTransport(site))


def test_account_from_url():
    assert YelloAccount.from_url("https://Acme.yello.co/login").host == HOST
    assert YelloAccount.from_url("acme.yello.co").default_company == "Acme"
    sso = YelloAccount.from_url(f"https://{SSO}/cas/login?service=https%3A%2F%2Facme.yello.co%2Flogin")
    assert sso.host == HOST
    for bad in (f"https://{SSO}/cas/login", "https://yello.co.evil.com/x", "https://example.com/", ""):
        with pytest.raises(ValueError):
            YelloAccount.from_url(bad)


def test_parsers():
    apps = parse_applications(home())
    assert [(a.id, a.requisition_id, a.title, a.status) for a in apps] == [
        ("11", "901", "Analyst & Data - Intern - Summer 2027", "Application Submitted"),
        ("12", "902", "Engineering - Intern", "Not selected"),
    ]
    assert parse_applications("<p>nothing</p>") == []
    assert is_logged_in(home()) and not is_logged_in(CAS) and is_login_page(CAS)
    assert hidden_form(CAS) == ("login", {"execution": "e1s1", "_eventId": "submit", "geolocation": ""})
    assert parse_schedule(home()) == {"invitations": [], "events": [], "interviews": []}
    filled = home(interviews='<div class="candidatePortal__eventCard">Final round 11-02-2026</div>')
    assert parse_schedule(filled)["interviews"] == ["Final round 11-02-2026"]


async def test_applications_log_in_and_cache_the_session(settings):
    site = FakeSite()
    async with client(site, settings) as c:
        apps = await c.applications()
    assert [(a.id, a.job_id, a.state) for a in apps] == [("11", "901", "active"), ("12", "902", "inactive")]
    assert site.logins == 1 and load_cached_session(settings.session_path(HOST))
    async with client(site, settings) as c:  # a stored session needs no new login
        await c.applications()
    assert site.logins == 1


async def test_expired_session_logs_in_again(settings):
    site = FakeSite()
    async with client(site, settings) as c:
        await c.applications()
    site.expire = True
    async with client(site, settings) as c:
        assert await c.schedule() == {"invitations": [], "events": [], "interviews": []}
    assert site.logins == 2


async def test_wrong_password(settings):
    with pytest.raises(AuthError):
        async with client(FakeSite(password="other"), settings):
            pass
