import html
import json
from datetime import date

import httpx
import pytest
from ats_core import mailbox
from mcp.server.mcpserver.exceptions import ToolError
from mcp_waas import auth, server
from mcp_waas.auth import AuthError
from mcp_waas.client import WaasClient, to_application
from mcp_waas.config import WaasSettings

KEY = "workatastartup.com"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = WaasSettings(cache_dir=tmp_path / "cache", config_dir=tmp_path / "config")
    monkeypatch.setenv("WAAS_CACHE_DIR", str(s.cache_dir))
    monkeypatch.setenv("WAAS_CONFIG_DIR", str(s.config_dir))
    return s


@pytest.fixture
def code(monkeypatch):
    monkeypatch.setattr(mailbox, "wait_for_code", lambda template, since: "123456")


def conversation(slug: str, *, state: str = "reviewing", replied: bool = False, job: int = 7) -> dict:
    messages = [{"from_candidate": True, "created_at": "2026-10-13T18:53:20.209Z", "message": "Hi!", "id": 1}]
    if replied:
        messages.append(
            {"from_candidate": False, "created_at": "2026-10-14T10:00:00Z", "message": "Hello", "id": 2}
        )
    return {
        "type": "stub", "id": slug, "state": state, "has_applied": True, "applied_job_title": None,
        "referenced_job_ids": [job], "company_messaged_at": None, "messages": messages,
        "company": {"id": 1, "name": slug.title()},
    }  # fmt: skip


def job_json(i: int, company: str = "Acme") -> dict:
    return {
        "id": i, "title": f"Intern {i}", "pretty_job_type": "Internship", "remote": "yes",
        "pretty_location_or_remote": "Remote (US)", "pretty_salary_range": "$1K", "description": "x" * 900,
        "show_path": f"https://www.workatastartup.com/jobs/{i}", "pretty_updated_at": "10/11/2026",
        "pretty_sub_type": "Full stack", "pretty_sponsors_visa": "none",
    }  # fmt: skip


def page(props: dict, *, logged_in: bool = True) -> httpx.Response:
    props = {"navBar": {"loggedIn": logged_in}, **props}
    data = html.escape(json.dumps({"component": "x", "props": props}))
    algolia = 'window.AlgoliaOpts = {"app":"APP1","key":"KEY1"};'
    body = f'<meta name="csrf-token" content="csrf1"><script>{algolia}</script><div data-page="{data}"></div>'
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


class FakeSite:
    """account.ycombinator.com (code login) and the bits of www.workatastartup.com we read."""

    def __init__(self):
        self.convs = [conversation("adam", job=76), conversation("ressl-ai", replied=True, job=113)]
        self.logged_in = False
        self.posts: list[tuple[str, dict]] = []
        self.hits = [("500", 11), ("501", 12), ("502", 13)]  # (job id, company id)
        self.searches: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "account.ycombinator.com":
            if req.method == "GET" and path == "/":
                return page({}, logged_in=False)
            if req.method == "GET" and path == "/authenticate_token":
                return httpx.Response(302, headers={"location": "https://www.workatastartup.com/set_auth"})
            body = json.loads(req.content)
            self.posts.append((path, body))
            if path == "/verify_otp":
                return httpx.Response(200, json={"token": "tok1"})
            return httpx.Response(200, json={"message": "sent"})
        if host.endswith("algolia.net"):
            self.searches.append(json.loads(req.content))
            hits = [{"objectID": j, "company_id": str(c)} for j, c in self.hits]
            return httpx.Response(200, json={"results": [{"hits": hits, "nbPages": 3}]})
        if path == "/set_auth":
            self.logged_in = True
            return httpx.Response(200, text="ok", headers={"set-cookie": "_bf_session_key=sess1; Path=/"})
        if path == "/companies/fetch":
            ids = json.loads(req.content)["ids"]

            # company 12 lists no jobs: its job must come from the job page
            def jobs_of(i: int) -> list[dict]:
                return [job_json(int(j)) for j, c in self.hits if c == i and i != 12]

            companies = [{"id": i, "name": f"Co{i}", "batch": "W26", "jobs": jobs_of(i)} for i in ids]
            return httpx.Response(200, json={"companies": companies})
        if not self.logged_in or req.headers.get("cookie", "").find("sess1") < 0:
            if path.startswith("/api/"):
                return httpx.Response(302, headers={"location": "https://www.workatastartup.com/"})
            return page({}, logged_in=False)
        if path == "/api/conversations":
            return httpx.Response(200, json=self.convs)
        if path.startswith("/api/conversations/"):
            slug = path.rsplit("/", 1)[1]
            return httpx.Response(200, json=next(c for c in self.convs if c["id"] == slug))
        if path == "/companies":
            return page({})
        if path.startswith("/jobs/"):
            jid = int(path.rsplit("/", 1)[1])
            return page(
                {
                    "job": {"id": jid, "title": f"Role {jid}", "location": "NYC"},
                    "company": {"name": "Acme"},
                    "appliedJobIds": [76],
                }
            )
        return httpx.Response(404)


def new_client(settings, site, key=KEY):
    return WaasClient(key, "me@x.com", "ycombinator", settings, httpx.MockTransport(site))


def test_to_application_maps_state_and_replies():
    app = to_application(conversation("adam"), KEY, "Residency")
    assert (app.id, app.company, app.job_id, app.title) == ("adam", "Adam", "7", "Residency")
    assert app.status == "Reviewing" and app.state == "active" and app.applied_on == date(2026, 10, 13)
    assert to_application(conversation("adam", replied=True), KEY).status == "Reviewing (company replied)"
    assert to_application(conversation("adam", state="rejected"), KEY).state == "inactive"


async def test_login_walks_the_yc_flow(code):
    site = FakeSite()
    async with httpx.AsyncClient(
        base_url=auth.BASE_URL, transport=httpx.MockTransport(site), follow_redirects=True
    ) as http:
        cookies = await auth.login(http, "me@x.com", "ycombinator")
    assert cookies["_bf_session_key"] == "sess1"
    assert [p for p, _ in site.posts] == ["/start_sign_in", "/login_link", "/verify_otp"]
    assert site.posts[2][1] == {"otp_code": "123456"} and site.posts[1][1]["email"] == "me@x.com"


async def test_login_failures_are_explained(code):
    def refuse(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/verify_otp":
            return httpx.Response(422, json={"error": "bad"})
        return FakeSite()(req)

    async with httpx.AsyncClient(base_url=auth.BASE_URL, transport=httpx.MockTransport(refuse)) as http:
        with pytest.raises(AuthError, match="rejected the login code"):
            await auth.login(http, "me@x.com", "ycombinator")

    def no_session(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/set_auth":
            return httpx.Response(200, text="ok")  # no cookie
        return FakeSite()(req)

    async with httpx.AsyncClient(
        base_url=auth.BASE_URL, transport=httpx.MockTransport(no_session), follow_redirects=True
    ) as http:
        with pytest.raises(AuthError, match="didn't open a session"):
            await auth.login(http, "me@x.com", "ycombinator")


async def test_applications_resolve_job_titles_and_reuse_session(settings, code):
    site = FakeSite()
    async with new_client(settings, site) as c:
        apps = await c.applications()
    assert {a.id: (a.title, a.status) for a in apps} == {
        "adam": ("Role 76", "Reviewing"),
        "ressl-ai": ("Role 113", "Reviewing (company replied)"),
    }
    saved = json.loads(settings.session_path(KEY).read_text(encoding="utf-8"))
    assert saved["_bf_session_key"] == "sess1"
    logins = len(site.posts)
    async with new_client(settings, site) as c:
        await c.applications()
    assert len(site.posts) == logins


async def test_expired_session_logs_in_once_more(settings, code):
    site = FakeSite()
    settings.session_path(KEY).parent.mkdir(parents=True)
    settings.session_path(KEY).write_text(json.dumps({"_bf_session_key": "stale"}), encoding="utf-8")
    async with new_client(settings, site) as c:
        apps = await c.applications()
    assert len(apps) == 2 and len(site.posts) == 3


async def test_search_builds_filters_and_falls_back_to_job_pages(settings, code):
    site = FakeSite()
    async with new_client(settings, site) as c:
        jobs, pages = await c.search_jobs("python", role="eng", job_type="intern", remote="yes", page=2)
    params = site.searches[0]["requests"][0]["params"]
    assert "page=1" in params and "query=python" in params
    assert "%28role%3Aeng%29+AND+%28remote%3Ayes%29+AND+%28job_type%3A%22intern%22%29" in params
    assert pages == 3 and [j.id for j in jobs] == ["500", "501", "502"]
    assert jobs[0].company == "Co11" and jobs[0].batch == "W26" and len(jobs[0].description or "") == 600
    assert jobs[1].title == "Role 501"  # company 12 listed nothing, so the job page filled it in


async def test_tools_end_to_end(settings, code, monkeypatch):
    site = FakeSite()
    real = WaasClient
    monkeypatch.setattr(
        server, "WaasClient", lambda *a, **k: real(*a, **k, transport=httpx.MockTransport(site))
    )
    with pytest.raises(ToolError, match="waas_add_account"):
        await server.waas_get_applications()
    assert await server.waas_add_account("Me@X.com") == {"account": KEY, "email": "me@x.com"}
    with pytest.raises(ToolError, match="isn't an email"):
        await server.waas_add_account("nope")

    res = await server.waas_get_applications(company="adam")
    assert [a.id for a in res.applications] == ["adam"] and not res.accounts[0].from_cache
    assert (await server.waas_get_applications()).accounts[0].from_cache

    assert (await server.waas_get_delta()).deltas[KEY].baseline
    site.convs[0]["state"] = "rejected"
    changed = (await server.waas_get_delta()).deltas[KEY].status_changed
    assert [(c.application.id, c.application.state) for c in changed] == [("adam", "inactive")]

    thread = await server.waas_get_conversation("ressl-ai")
    assert [m.from_candidate for m in thread.messages] == [True, False] and thread.company == "Ressl-Ai"
    with pytest.raises(ToolError, match="isn't an application id"):
        await server.waas_get_conversation("../x")

    found = await server.waas_search_jobs("py", role="eng")
    assert found.total_pages == 3 and len(found.jobs) == 3
    with pytest.raises(ToolError, match="plain word"):
        await server.waas_search_jobs(role='eng) OR (x:"')
    job = await server.waas_get_job("76")
    assert job.applied and job.title == "Role 76"
    with pytest.raises(ToolError, match="isn't a job id"):
        await server.waas_get_job("abc")

    assert await server.waas_delete_account()
    assert not settings.session_path(KEY).exists()


def test_browser_headers_send_an_html_accept():
    assert "text/html" in auth.browser_headers()["accept"]  # www answers 406 to a bare */*
