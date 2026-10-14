import json
from datetime import date

import httpx
import pytest
from ats_core import CredentialStore, mailbox
from mcp_oracle import auth, server
from mcp_oracle.accounts import OracleAccount, discover_api_host
from mcp_oracle.auth import AuthError
from mcp_oracle.client import OracleClient, to_application
from mcp_oracle.config import OracleSettings

SIGN_IN = "https://careers.acme.com/en/sites/CX_1/my-profile/sign-in?x=1"
ACCT = OracleAccount(host="careers.acme.com", site="CX_1", api_host="eg.fa.us2.oraclecloud.com")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = OracleSettings(cache_dir=tmp_path / "cache", config_dir=tmp_path / "config")
    monkeypatch.setenv("ORACLE_CACHE_DIR", str(s.cache_dir))
    monkeypatch.setenv("ORACLE_CONFIG_DIR", str(s.config_dir))
    return s


def test_account_from_urls():
    host, site = OracleAccount.host_and_site(SIGN_IN)
    assert (host, site) == ("careers.acme.com", "CX_1")
    hosted = OracleAccount.from_url(
        "https://eg.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_9/jobs"
    )
    assert hosted.api_host == hosted.host == "eg.fa.us2.oraclecloud.com" and hosted.site == "CX_9"
    with pytest.raises(ValueError, match="/sites/"):
        OracleAccount.host_and_site("https://careers.acme.com/en/jobs")
    with pytest.raises(ValueError, match="discover"):
        OracleAccount.from_url(SIGN_IN)


def test_defaults_from_host():
    assert ACCT.default_company == "Acme"
    assert ACCT.default_mail_template == "acme"
    two_words = OracleAccount(host="jobs.acme-corp.com", site="CX_1", api_host="x")
    assert two_words.default_company == "Acme Corp" and two_words.default_mail_template == "acmecorp"
    assert ACCT.meta("typed")["auth"] == "email-code"


def test_discover_api_host_from_page(monkeypatch):
    html = '<script>var u="https://EG.fa.us2.oraclecloud.com/hcmRestApi";</script>'
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(200, text=html, request=httpx.Request("GET", a[0]))
    )
    assert discover_api_host(SIGN_IN).api_host == "eg.fa.us2.oraclecloud.com"
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(200, text="nothing", request=httpx.Request("GET", a[0]))
    )
    with pytest.raises(ValueError, match="Oracle Recruiting Cloud"):
        discover_api_host(SIGN_IN)


def make_http(handler):
    return httpx.AsyncClient(
        base_url=f"https://{ACCT.api_host}",
        transport=httpx.MockTransport(handler),
        headers=auth.base_headers(ACCT),
    )


async def test_login_requests_code_then_verifies(monkeypatch):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append((body, dict(req.headers)))
        if "Email" in body and "ShortCode" not in body:
            return httpx.Response(201, json={"ChallengeFlag": True})
        return httpx.Response(201, json={"AccessCode": "ac-1", "CandidateNumber": "7"})

    seen = {}
    monkeypatch.setattr(
        mailbox, "wait_for_code", lambda template, since: seen.setdefault("t", template) and "123456"
    )
    async with make_http(handler) as http:
        session = await auth.login(http, ACCT, "me@x.com", "acme")
    assert session.access_code == "ac-1" and seen["t"] == "acme"
    first, second = calls
    assert first[0]["SiteNumber"] == "CX_1" and first[0]["Email"] == "me@x.com"
    assert first[1]["ora-irc-rest-action"] == "CSS_ACCESS:EMAIL"
    assert second[0] == {"ShortCode": "123456", "ChallengeFlag": True, "Email": "me@x.com"}
    assert first[1]["ora-irc-cx-userid"] == second[1]["ora-irc-cx-userid"] == session.user_id


async def test_login_failures_are_explained(monkeypatch):
    monkeypatch.setattr(mailbox, "wait_for_code", lambda template, since: "000000")
    async with make_http(lambda r: httpx.Response(201, json={"ChallengeFlag": False})) as http:
        with pytest.raises(AuthError, match="didn't send a login code"):
            await auth.login(http, ACCT, "me@x.com", "acme")

    def reject(req):
        if "ShortCode" in json.loads(req.content):
            return httpx.Response(400, json={})
        return httpx.Response(201, json={"ChallengeFlag": True})

    async with make_http(reject) as http:
        with pytest.raises(AuthError, match="rejected the login code"):
            await auth.login(http, ACCT, "me@x.com", "acme")


RAW = {
    "SubmissionId": 300001830198226, "RequisitionNumber": "26011781", "JobTitle": " Intern, Engineering ",
    "Status": "Application Under Review", "ActiveFlag": True, "SubmissionDate": "2026-08-19T03:13:44+00:00",
    "assessmentLinks": [], "BackgroundCheckLinks": [], "UnscheduledInterviewRequests": [{"x": 1}],
}  # fmt: skip


def test_to_application_maps_fields():
    app = to_application(RAW, "careers.acme.com")
    assert (app.id, app.job_id, app.title) == ("300001830198226", "26011781", "Intern, Engineering")
    assert app.state == "active" and app.applied_on == date(2026, 8, 19) and app.has_pending_tasks
    closed = to_application({**RAW, "ActiveFlag": False, "UnscheduledInterviewRequests": []}, "a")
    assert closed.state == "inactive" and not closed.has_pending_tasks


async def test_client_pages_relogs_in_on_401_and_adds_drafts(settings, monkeypatch):
    state = {"logins": 0, "app_calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("recruitingCEVerificationTokens"):
            body = json.loads(req.content)
            if "ShortCode" in body:
                state["logins"] += 1
                return httpx.Response(201, json={"AccessCode": f"ac-{state['logins']}"})
            return httpx.Response(201, json={"ChallengeFlag": True})
        if req.headers.get("ora-irc-access-code") == "stale":
            return httpx.Response(401, json={})
        assert req.method == "GET"
        if path.endswith("recruitingCEJobApplications"):
            state["app_calls"] += 1
            offset = int(req.url.params["offset"])
            items = [{**RAW, "SubmissionId": offset + 1}]
            return httpx.Response(200, json={"items": items, "hasMore": offset == 0})
        return httpx.Response(
            200,
            json={
                "items": [{"APPDraftId": 5, "RequisitionNumber": "9"}, {"APPDraftId": 6, "SubmissionId": 1}]
            },
        )

    monkeypatch.setattr(mailbox, "wait_for_code", lambda template, since: "123456")
    auth.save_session(settings.session_path(ACCT.key), auth.Session(user_id="u", access_code="stale"))
    async with OracleClient(ACCT, "me@x.com", "acme", settings, transport=httpx.MockTransport(handler)) as c:
        apps = await c.applications()
    assert state["logins"] == 1  # the stale cached session triggered exactly one re-login
    assert [a.id for a in apps if not a.is_draft] == ["1", "500"]  # two pages
    drafts = [a for a in apps if a.is_draft]
    assert [d.id for d in drafts] == ["draft-5"] and drafts[
        0
    ].status == "Draft"  # draft with SubmissionId excluded
    assert auth.load_session(settings.session_path(ACCT.key)).access_code == "ac-1"  # type: ignore[union-attr]


async def test_server_add_account_and_selection(settings, monkeypatch):
    monkeypatch.setattr(server, "discover_api_host", lambda url: ACCT)
    out = await server.oracle_add_account(SIGN_IN, "me@x.com", company="Acme Corp")
    assert out == {"account": "careers.acme.com", "company": "Acme Corp", "email": "me@x.com", "site": "CX_1"}
    store = CredentialStore("oracle", settings.config_dir)
    assert store.get("careers.acme.com") is None  # nothing secret was stored
    assert store.list()["careers.acme.com"]["mail_template"] == "acme"
    assert server.select_accounts(settings, company="acme corp") == ["careers.acme.com"]
    acct, email, template = server.load_account(settings, "careers.acme.com")
    assert (acct.site, email, template) == ("CX_1", "me@x.com", "acme")
    assert await server.oracle_delete_account("careers.acme.com")


async def test_server_reports_mailbox_problem_per_account(settings, monkeypatch):
    monkeypatch.setattr(server, "discover_api_host", lambda url: ACCT)
    await server.oracle_add_account(SIGN_IN, "me@x.com")
    challenge = httpx.MockTransport(lambda r: httpx.Response(201, json={"ChallengeFlag": True}))
    monkeypatch.setattr(
        server,
        "OracleClient",
        lambda acct, email, tpl, st: OracleClient(acct, email, tpl, st, transport=challenge),
    )
    res = await server.oracle_get_applications(refresh=True)
    assert "work-mcp mailbox" in (res.accounts[0].error or "")  # reported per account, not a crash
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="No registered company"):
        await server.oracle_get_applications(company="zzz")
