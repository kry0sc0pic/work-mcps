import html
import json
import sqlite3
from datetime import date

import httpx
import pytest
from ats_core import Application, ApplicationCache, mailbox
from mcp_greenhouse import auth, server
from mcp_greenhouse.auth import AuthError
from mcp_greenhouse.client import GreenhouseClient, to_application
from mcp_greenhouse.config import GreenhouseSettings


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = GreenhouseSettings(cache_dir=tmp_path / "cache", config_dir=tmp_path / "config")
    monkeypatch.setenv("GREENHOUSE_CACHE_DIR", str(s.cache_dir))
    monkeypatch.setenv("GREENHOUSE_CONFIG_DIR", str(s.config_dir))
    return s


def raw_app(i: int, *, inactive: bool = False, company: str = "Acme") -> dict:
    return {
        "id": i, "company_name": company, "job_post_id": 900 + i, "job_title": f" Engineer {i} ",
        "applied_at": "2026-08-19T21:20:01+05:30", "inactive": inactive, "stages": [],
    }  # fmt: skip


JOB = {
    "id": 55, "title": " Intern ", "companyName": "Globex", "locations": ["Palo Alto, CA", "Remote"],
    "workType": "hybrid", "payRanges": "$1 - $2", "viewJobPath": "/jobs/globex/55", "viewed": True,
    "firstPublished": "2026-10-14T23:43:28Z", "appliedAt": None, "summary": None, "description": None,
}  # fmt: skip


class FakeSite:
    """Just enough of my.greenhouse.io: sign-in page, code login, paged applications.json."""

    def __init__(self, active=3, inactive=1, per_page=2):
        self.active = [raw_app(i) for i in range(active)]
        self.inactive = [raw_app(100 + i, inactive=True) for i in range(inactive)]
        self.per_page = per_page
        self.logged_in = False
        self.posts: list[tuple[str, dict, dict]] = []
        self.gets = 0
        self.searches: list[tuple[dict, dict]] = []
        self.patches: list[dict] = []
        self.geo: list[dict] = []
        self.extra_history: dict[int, list[dict]] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and path == "/users/sign_in":
            page = html.escape(json.dumps({"component": "login", "version": "v1", "props": {}}))
            return httpx.Response(
                200,
                text=f'<div data-page="{page}"></div>',
                headers={"set-cookie": "MYGREENHOUSE-XSRF-TOKEN=tok1; Path=/", "content-type": "text/html"},
            )
        if req.method == "POST":
            body = json.loads(req.content)
            self.posts.append((path, body, dict(req.headers)))
            if path == "/users/sign_in":
                return httpx.Response(200, json={"component": "login", "props": {"loginError": None}})
            self.logged_in = True
            return httpx.Response(
                200,
                json={"component": "dashboard", "props": {"currentUserId": 5}},
                headers={"set-cookie": "_session_id=sess1; Path=/"},
            )
        if req.url.host == "geo.test":
            feature = {
                "geometry": {"coordinates": [-122.43, 37.78]},
                "properties": {
                    "layer": "locality", "locality": "San Francisco", "region": "California",
                    "region_a": "CA", "country": "United States", "country_code": "US",
                },
            }  # fmt: skip
            self.geo.append(dict(req.url.params))
            return httpx.Response(
                200, json={"features": [feature] if "nowhere" not in req.url.query.decode() else []}
            )
        if path == "/jobs/search" and "x-inertia" not in req.headers:
            props = {"locationControlSettings": {"baseUrl": "https://geo.test/", "apiKey": "k"}}
            page = html.escape(json.dumps({"component": "jobs", "version": "v1", "props": props}))
            return httpx.Response(
                200, text=f'<div data-page="{page}"></div>', headers={"content-type": "text/html"}
            )
        if path == "/jobs/search":
            self.searches.append((dict(req.url.params), dict(req.headers)))
            props = {"page": 1, "moreResultsAvailable": True, "jobPosts": [JOB]}
            return httpx.Response(200, json={"component": "jobs", "props": props})
        if path == "/recommendations":
            return httpx.Response(200, json=[{**JOB, "description": "x" * 900}])
        if req.method == "PATCH" and path.endswith("/mark_inactive"):
            self.patches.append(dict(req.headers))
            aid = path.split("/")[2]
            for a in self.active:
                if str(a["id"]) == aid:
                    a["inactive"] = True
                    self.active.remove(a)
                    self.inactive.append(a)
                    break
            return httpx.Response(200, json={"success": True})
        if path.startswith("/applications/"):
            aid = int(path.split("/")[2])
            hist = [{"date": "2026-08-01", "type": "applied"}] + self.extra_history.get(aid, [])
            raw = next(a for a in self.active + self.inactive if a["id"] == aid)
            return httpx.Response(
                200,
                json={
                    "component": "applications",
                    "props": {"selected_application": {**raw, "activityHistory": hist}},
                },
            )
        if path == "/applications.json":
            self.gets += 1
            if not self.logged_in or "sess1" not in req.headers.get("cookie", ""):
                return httpx.Response(200, text="<html>sign in</html>", headers={"content-type": "text/html"})
            q = req.url.params
            key, rows = ("inactive", self.inactive) if q.get("inactive_only") else ("active", self.active)
            n = int(q["page"])
            pages = max(1, -(-len(rows) // self.per_page))
            chunk = rows[(n - 1) * self.per_page : n * self.per_page]
            return httpx.Response(
                200, json={"total_applications": 4, key: {"applications": chunk, "total_pages": pages}}
            )
        return httpx.Response(404)


@pytest.fixture
def code(monkeypatch):
    monkeypatch.setattr(mailbox, "wait_for_code", lambda template, since: "12345678")


def test_builtin_template_reads_eight_digits():
    t = mailbox.load_template("greenhouse")
    assert mailbox.find_code("Your code is 12345678", t.pattern) == "12345678"
    assert mailbox.find_code("Order 123456", t.pattern) is None


def test_to_application_maps_fields():
    app = to_application(raw_app(7), "my.greenhouse.io")
    assert (app.id, app.job_id, app.title, app.company) == ("7", "907", "Engineer 7", "Acme")
    assert app.status == "Active" and app.state == "active" and app.applied_on == date(2026, 8, 19)
    closed = to_application(raw_app(8, inactive=True), "k")
    assert closed.status == "Inactive" and closed.state == "inactive"


async def test_login_sends_inertia_headers_and_csrf(code):
    site = FakeSite()
    async with httpx.AsyncClient(
        base_url=auth.BASE_URL, transport=httpx.MockTransport(site), follow_redirects=True
    ) as http:
        cookies = await auth.login(http, "me@x.com", "greenhouse")
    assert cookies["_session_id"] == "sess1"
    (p1, b1, h1), (p2, b2, h2) = site.posts
    assert (p1, p2) == ("/users/sign_in", "/users/submit_code")
    assert b1["email"] == "me@x.com" and b2["code"] == "12345678" and b2["email"] == "me@x.com"
    assert h1["x-csrf-token"] == "tok1" and h1["x-inertia"] == "true" and h1["x-inertia-version"] == "v1"


async def test_login_failures_are_explained(code):
    def reject_code(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/users/submit_code":
            return httpx.Response(200, json={"props": {"currentUserId": None}})
        return FakeSite()(req)

    async with httpx.AsyncClient(base_url=auth.BASE_URL, transport=httpx.MockTransport(reject_code)) as http:
        with pytest.raises(AuthError, match="rejected the login code"):
            await auth.login(http, "me@x.com", "greenhouse")

    def unknown_email(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json={"props": {"loginError": "nope"}})
        return FakeSite()(req)

    async with httpx.AsyncClient(
        base_url=auth.BASE_URL, transport=httpx.MockTransport(unknown_email)
    ) as http:
        with pytest.raises(AuthError, match="refused the sign-in"):
            await auth.login(http, "me@x.com", "greenhouse")


async def test_client_pages_both_lists_and_caches_session(settings, code):
    site = FakeSite()
    async with GreenhouseClient(
        "my.greenhouse.io", "me@x.com", "greenhouse", settings, httpx.MockTransport(site)
    ) as c:
        apps = await c.applications()
    assert sorted(a.id for a in apps) == ["0", "1", "100", "2"]
    assert sum(1 for a in apps if a.state == "inactive") == 1
    saved = json.loads(settings.session_path("my.greenhouse.io").read_text(encoding="utf-8"))
    assert saved["_session_id"] == "sess1"
    logins = len(site.posts)

    async with GreenhouseClient(
        "my.greenhouse.io", "me@x.com", "greenhouse", settings, httpx.MockTransport(site)
    ) as c:
        await c.applications()
    assert len(site.posts) == logins  # the cached session was reused


async def test_expired_session_logs_in_once_more(settings, code):
    site = FakeSite()
    settings.session_path("k").parent.mkdir(parents=True)
    settings.session_path("k").write_text(json.dumps({"_session_id": "stale"}), encoding="utf-8")
    async with GreenhouseClient("k", "me@x.com", "greenhouse", settings, httpx.MockTransport(site)) as c:
        apps = await c.applications()
    assert len(apps) == 4 and len(site.posts) == 2
    assert json.loads(settings.session_path("k").read_text(encoding="utf-8"))["_session_id"] == "sess1"


async def test_fresh_session_that_is_rejected_gives_up(settings, code):
    def always_html(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/applications.json":
            return httpx.Response(200, text="<html/>", headers={"content-type": "text/html"})
        return FakeSite()(req)

    async with GreenhouseClient(
        "k", "me@x.com", "greenhouse", settings, httpx.MockTransport(always_html)
    ) as c:
        with pytest.raises(AuthError, match="rejected a freshly created session"):
            await c.applications()


def test_cache_keeps_company_and_migrates_old_db(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:  # a database from before the company column existed
        conn.execute(
            "create table applications (account text not null, id text not null, job_id text, title text,"
            " status text, state text, applied_on text, can_withdraw integer, is_draft integer,"
            " has_pending_tasks integer, first_seen text, last_seen text, primary key (account, id))"
        )
    conn.close()
    cache = ApplicationCache(path)
    cache.replace(
        "k",
        [
            Application(
                id="1", account="k", company="Acme", job_id="9", title="T", status="Active", state="active"
            )
        ],
    )
    assert cache.load(["k"])[0].company == "Acme"


def test_filters_by_company_title_and_state():
    apps = [
        Application(id="1", company="Stripe", job_id="", title="Intern", status="Active", state="active"),
        Application(id="2", company="Acme", job_id="", title="Engineer", status="Inactive", state="inactive"),
    ]
    assert [a.id for a in server.filter_applications(apps, company="stri")] == ["1"]
    assert [a.id for a in server.filter_applications(apps, state="inactive")] == ["2"]
    assert [a.id for a in server.filter_applications(apps, title="engin")] == ["2"]


async def test_tools_end_to_end(settings, code, monkeypatch):
    site = FakeSite()
    real = GreenhouseClient
    monkeypatch.setattr(
        server, "GreenhouseClient", lambda *a, **k: real(*a, **k, transport=httpx.MockTransport(site))
    )
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="greenhouse_add_account"):
        await server.greenhouse_get_applications()
    added = await server.greenhouse_add_account("Me@X.com")
    assert added == {"account": "my.greenhouse.io", "email": "me@x.com"}
    with pytest.raises(ToolError, match="isn't an email"):
        await server.greenhouse_add_account("nope")

    res = await server.greenhouse_get_applications(company="acme", state="active")
    assert len(res.applications) == 3 and not res.accounts[0].from_cache and res.accounts[0].error is None
    again = await server.greenhouse_get_applications()
    assert again.accounts[0].from_cache and len(again.applications) == 4

    first = await server.greenhouse_get_delta()
    assert first.deltas["my.greenhouse.io"].baseline
    site.active.pop(0)
    second = await server.greenhouse_get_delta()
    assert [a.id for a in second.deltas["my.greenhouse.io"].removed] == ["0"]
    assert await server.greenhouse_delete_account()
    assert not settings.session_path("my.greenhouse.io").exists()


def test_status_text_depends_on_sharing():
    from mcp_greenhouse.client import status_text

    plain = {"inactive": False, "sharingStatus": False}
    assert status_text(plain, None) == "Active"
    assert status_text({**plain, "inactive": True}, None) == "Inactive"
    shared = {"inactive": False, "sharingStatus": True}
    applied = {"activityHistory": [{"type": "applied"}], "stages": []}
    assert status_text(shared, applied) == "Applied"
    assert status_text({**shared, "inactive": True}, applied) == "Inactive"
    assert status_text(shared, {"activityHistory": [{"type": "applied"}, {"type": "rejected"}]}) == "Rejected"
    assert status_text(shared, {"stages": [{"name": "Phone screen"}]}) == "Phone screen"
    assert status_text(shared, None) == "Active"  # detail view unavailable


async def test_sharing_applications_read_their_detail(settings, code):
    site = FakeSite(active=2, inactive=0)
    for a in site.active:
        a["sharingStatus"] = a["id"] == 1
    site.extra_history[1] = [{"date": "2026-09-01", "type": "interview"}]
    async with GreenhouseClient("k", "me@x.com", "greenhouse", settings, httpx.MockTransport(site)) as c:
        apps = {a.id: a.status for a in await c.applications()}
    assert apps == {"0": "Active", "1": "Interview"}


async def test_mark_inactive_sends_csrf_and_refreshes(settings, code, monkeypatch):
    site = FakeSite(active=2, inactive=0)
    real = GreenhouseClient
    monkeypatch.setattr(
        server, "GreenhouseClient", lambda *a, **k: real(*a, **k, transport=httpx.MockTransport(site))
    )
    from mcp.server.mcpserver.exceptions import ToolError

    await server.greenhouse_add_account("me@x.com")
    await server.greenhouse_get_applications()
    with pytest.raises(ToolError, match="isn't one of"):
        await server.greenhouse_mark_inactive("999")
    done = await server.greenhouse_mark_inactive("1")
    assert done.state == "inactive" and done.status == "Inactive"
    assert site.patches[0]["x-csrf-token"] == "tok1" and "application/json" in site.patches[0]["accept"]
    cached = await server.greenhouse_get_applications(state="inactive")
    assert [a.id for a in cached.applications] == ["1"]


async def test_recommendations_and_search(settings, code, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    site = FakeSite()
    real = GreenhouseClient
    monkeypatch.setattr(
        server, "GreenhouseClient", lambda *a, **k: real(*a, **k, transport=httpx.MockTransport(site))
    )
    await server.greenhouse_add_account("me@x.com")
    recs = await server.greenhouse_get_recommendations(limit=1)
    assert recs[0].company == "Globex" and recs[0].description and len(recs[0].description) == 600
    assert recs[0].url == "https://my.greenhouse.io/jobs/globex/55" and recs[0].work_type == "hybrid"
    assert recs[0].location == "Palo Alto, CA; Remote" and recs[0].posted_on == date(2026, 10, 14)

    res = await server.greenhouse_search_jobs("intern", location="San Francisco", page=2)
    assert res.more_results and res.page == 2 and res.jobs[0].title == "Intern"
    params, headers = site.searches[0]
    assert params == {
        "query": "intern", "location": "San Francisco, California, United States", "lat": "37.78",
        "lon": "-122.43", "location_type": "city", "country_short_name": "US", "state_short_name": "CA",
        "page": "2",
    }  # fmt: skip
    assert site.geo[0]["text"] == "San Francisco" and site.geo[0]["api_key"] == "k"
    with pytest.raises(ToolError, match="couldn't find a place"):
        await server.greenhouse_search_jobs("intern", location="nowhere")
    assert headers["x-inertia-version"] == "v1" and "jobPosts" in headers["x-inertia-partial-data"]
