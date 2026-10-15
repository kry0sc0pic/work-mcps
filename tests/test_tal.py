import base64
import hashlib
import json
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest
from ats_core import ApplicationCache, Credential
from mcp.server.mcpserver.exceptions import ToolError
from mcp_tal.accounts import TalAccount
from mcp_tal.auth import AuthError, load_cached_session
from mcp_tal.client import TalClient, status_text
from mcp_tal.config import TalSettings
from mcp_tal.parse import (
    html_text,
    needs_check,
    parse_application,
    parse_application_list,
    parse_correspondence,
    parse_date,
    parse_email,
)
from mcp_tal.server import (
    credential_store,
    tal_delete_credentials,
    tal_get_application,
    tal_get_applications,
    tal_get_delta,
    tal_get_message,
    tal_list_accounts,
)

HOST = "acme.tal.net"
CRED = Credential(username="me@x.com", password="Pw-Case1")
SALT = "abc?expires=1"
NUMBER = 37


def _page(body: str, logged_in: bool = True) -> str:
    menu = '<a class="logout_button" href="#">Logout</a>' if logged_in else '<form id="logout_form"></form>'
    main = f"<div id='main-content'>{body}</div>"
    return f'<html><body>{menu}<div class="body-content">{main}</div></body></html>'


LOGIN_FORM = """<form id="login_form" method="post" action="https://acme.tal.net/vx/brand-2/xf-1/candidate/login">
<input type="hidden" name="formtt_uuid" value="u-1" />
<input type="hidden" name="__vxXSRF_Token" value="t0k" />
<input type="text" name="user" /><input type="password" name="password" /></form>"""

CHECK_PAGE = """<html><altcha-widget challengeurl="?oleeoProtectCaptcha=1"></altcha-widget>
<form method="post"><input type="hidden" name="originalRequestToken" value="orig.jwt.tok"></form></html>"""

LIST = """<table class='app-listing-table'><tbody>
<tr><td>Summer Analyst (London)</td><td>Application Update</td><td class="app_link">
<a class="subject" href="https://acme.tal.net/vx/brand-2/user-1/xf-1/candidate/application/101">Review</a></td></tr>
<tr><td>Summer Analyst (Paris)</td><td>Application Update</td><td class="app_link">
<a class="subject" href="https://acme.tal.net/vx/brand-2/user-1/xf-1/candidate/application/102">Review</a></td></tr>
</tbody></table>"""


def detail(title: str, message: str) -> str:
    return (
        f'<h1 class="section app-heading">Application For <strong class="app-position">{title}</strong></h1>'
        f"<div class='app-status'><h2><strong>{title}: </strong> <b>Application Update</b></h2></div>"
        f"<div class='app-status-desc'>{message}</div>"
    )


COMMS = """<table class="table history-of-comms"><tbody>
<tr><td>15/10/2026</td>
<td><a class="subject" href="x/candidate/application/101/correspondence/email/9001">Update</a></td>
<td>Message has been sent</td></tr></tbody></table>"""

EMAIL = """<h2 class="section">Email Details Update</h2><span class="corr">Date Sent</span>
<span>15/10/2026, 15:50</span><div class="rich_text"><p>Hello&#160;there</p><p>Bye</p></div><hr />"""


class FakeSite:
    def __init__(self, *, check: bool = False, password: str = CRED.password):
        self.check = check
        self.password = password
        self.cookies: set[str] = set()  # server-side valid cookie values
        self.logins = 0
        self.message = "<p>Your application is currently under review.</p>"
        self.expire = False

    def authed(self, req: httpx.Request) -> bool:
        jar = req.headers.get("cookie", "")
        passed = "bypassCaptcha=ok" in jar or not self.check
        return "wcn_session=sess" in jar and passed and not self.expire

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if (
            path == "/candidate/login"
            and req.method == "GET"
            and "oleeoProtectCaptcha" in req.url.query.decode()
        ):
            return httpx.Response(
                200,
                json={
                    "algorithm": "SHA-512",
                    "challenge": hashlib.sha512(f"{SALT}{NUMBER}".encode()).hexdigest(),
                    "maxnumber": 100,
                    "salt": SALT,
                    "signature": "sig",
                },
            )
        if path == "/candidate/login" and req.method == "GET":
            if self.check and "bypassCaptcha=ok" not in req.headers.get("cookie", ""):
                return httpx.Response(200, text=CHECK_PAGE)
            return httpx.Response(200, text=_page(LOGIN_FORM, logged_in=False))
        if path == "/candidate/login" and req.method == "POST":  # the Quick Check answer
            form = parse_qs(req.content.decode())
            answer = json.loads(base64.b64decode(form["altcha"][0]))
            assert answer["number"] == NUMBER and form["originalRequestToken"] == ["orig.jwt.tok"]
            return httpx.Response(
                200,
                text=_page(LOGIN_FORM, logged_in=False),
                headers={"set-cookie": "bypassCaptcha=ok; Path=/"},
            )
        if path.endswith("/candidate/login") and req.method == "POST":
            form = parse_qs(req.content.decode())
            assert form["__vxXSRF_Token"] == ["t0k"] and form["formtt_uuid"] == ["u-1"]
            if form["user"] != [CRED.username] or form["password"] != [self.password]:
                return httpx.Response(200, text=_page(LOGIN_FORM, logged_in=False))
            self.logins += 1
            self.expire = False
            return httpx.Response(
                302, headers={"location": "/candidate", "set-cookie": "wcn_session=sess; Path=/"}
            )
        if not self.authed(req):
            return httpx.Response(200, text=_page("signed out", logged_in=False))
        pages = {
            "/candidate": "home",
            "/candidate/application": LIST,
            "/candidate/application/101": detail("Summer Analyst (London)", self.message),
            "/candidate/application/102": detail(
                "Summer Analyst (Paris)", "<p>Unfortunately we cannot proceed.</p>"
            ),
            "/candidate/application/101/correspondence": COMMS,
            "/candidate/application/101/opportunity": "<h1>Review Details</h1><p>City</p><p>London</p>",
            "/candidate/application/101/correspondence/email/9001": EMAIL,
        }
        if path in pages:
            return httpx.Response(200, text=_page(pages[path]))
        return httpx.Response(404, text="nope")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = TalSettings(cache_dir=tmp_path / "cache", config_dir=tmp_path / "config")
    monkeypatch.setattr("mcp_tal.server.TalSettings", lambda: s)
    return s


def client(site: FakeSite, settings: TalSettings) -> TalClient:
    return TalClient(TalAccount(host=HOST), CRED, settings, transport=httpx.MockTransport(site))


def test_account_from_url():
    assert TalAccount.from_url("https://Acme.tal.net/vx/lang-en-GB/candidate/login").host == HOST
    assert TalAccount.from_url("acme.tal.net").default_company == "Acme"
    for bad in ("https://example.com/candidate", "https://tal.net.evil.com/x", ""):
        with pytest.raises(ValueError):
            TalAccount.from_url(bad)


def test_parsers():
    assert [(a.id, a.title, a.status) for a in parse_application_list(LIST)] == [
        ("101", "Summer Analyst (London)", "Application Update"),
        ("102", "Summer Analyst (Paris)", "Application Update"),
    ]
    page = parse_application(detail("Summer Analyst (London)", "<p>One.</p><p>Two.</p>"))
    assert (page.title, page.heading, page.message) == (
        "Summer Analyst (London)",
        "Application Update",
        "One.\n\nTwo.",
    )
    assert status_text(page.heading, page.message) == "Application Update: One."
    msg = parse_correspondence(COMMS)[0]
    assert (msg.id, msg.sent_on, msg.subject) == ("9001", date(2026, 10, 15), "Update")
    email = parse_email(EMAIL)
    assert (email.sent_at, email.subject, email.body) == ("15/10/2026, 15:50", "Update", "Hello there\n\nBye")
    assert parse_date("11/08/2026") == date(2026, 8, 11) and parse_date("junk") is None
    assert needs_check(CHECK_PAGE) and not needs_check(LOGIN_FORM)
    assert html_text("<script>x()</script>a<br>b") == "a\nb"


async def test_applications_log_in_and_cache_the_session(settings):
    site = FakeSite()
    async with client(site, settings) as c:
        apps = await c.applications()
    assert [(a.id, a.state) for a in apps] == [("101", "active"), ("102", "inactive")]
    assert apps[0].status == "Application Update: Your application is currently under review."
    assert apps[0].account == HOST and apps[0].applied_on is None
    assert site.logins == 1
    assert load_cached_session(settings.session_path(HOST)) is not None
    async with client(site, settings) as c:  # reuses the saved cookies
        await c.applications()
    assert site.logins == 1


async def test_login_solves_the_quick_check(settings):
    site = FakeSite(check=True)
    async with client(site, settings) as c:
        assert len(await c.applications()) == 2
    assert site.logins == 1


async def test_wrong_password(settings):
    with pytest.raises(AuthError, match="login failed"):
        async with client(FakeSite(password="other"), settings):
            pass


async def test_expired_session_logs_in_again(settings):
    site = FakeSite()
    async with client(site, settings) as c:
        await c.applications()
        site.expire = True
        await c.applications()
    assert site.logins == 2


async def test_detail_and_email(settings):
    async with client(FakeSite(), settings) as c:
        d = await c.application("101")
        assert d.title == "Summer Analyst (London)" and d.communications[0].id == "9001"
        assert "London" in d.details
        assert (await c.email("101", "9001")).body == "Hello there\n\nBye"
        with pytest.raises(ValueError):
            await c.email("101", "../x")


async def test_tools_end_to_end(settings, monkeypatch):
    site = FakeSite()
    credential_store(settings).save(HOST, CRED, meta={"company": "Acme", "source": "cli"})
    real = TalClient.__init__

    def init(self, account, cred, s, transport=None):
        real(self, account, cred, s, transport=httpx.MockTransport(site))

    monkeypatch.setattr(TalClient, "__init__", init)
    assert HOST in await tal_list_accounts()
    res = await tal_get_applications(company="acme")
    assert [a.id for a in res.applications] == ["101", "102"] and res.applications[0].company == "Acme"
    assert not res.accounts[0].from_cache and site.logins == 1
    assert (await tal_get_applications()).accounts[0].from_cache
    first = await tal_get_delta()
    assert first.deltas[HOST].baseline
    site.message = "<p>Interview invitation.</p>"
    changed = (await tal_get_delta()).deltas[HOST].status_changed
    assert [c.application.id for c in changed] == ["101"]
    assert (await tal_get_application("101")).communications[0].id == "9001"
    assert (await tal_get_message("101", "9001")).subject == "Update"
    with pytest.raises(ToolError):
        await tal_get_message("101", "x")
    assert await tal_delete_credentials(HOST)
    assert ApplicationCache(settings.db_path).load([HOST]) == []
