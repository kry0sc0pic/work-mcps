"""Work at a Startup candidate client. Logs in with an emailed code as needed."""

import asyncio
import html
import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from ats_core import Application, Job

from mcp_waas.auth import (
    BASE_URL,
    HOST,
    AuthError,
    browser_headers,
    cookies_for,
    csrf_token,
    load_cached_session,
    login,
    save_session,
)
from mcp_waas.config import WaasSettings

_PAGE = re.compile(r'data-page="([^"]*)"')
_ALGOLIA = re.compile(r"window\.AlgoliaOpts\s*=\s*(\{.*?\});", re.S)
INDEX = "WaaSPublicCompanyJob_created_at_desc_production"
# The site files a rejection under a state we haven't seen (only "reviewing" has been). These words in
# the state mark the application as finished.
_CLOSED = ("reject", "declin", "pass", "archiv", "clos", "withdr", "not_", "no_")


def parse_date(s: str | None) -> date | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).date()
    except ValueError:
        return None


def to_application(raw: dict[str, Any], account: str, job_title: str = "") -> Application:
    """One conversation is one application: the candidate's first message to a company, and the thread
    that follows. `state` is the site's own word for where it stands ("reviewing" is the only one seen)."""
    state = str(raw.get("state") or "").strip().lower()
    messages = raw.get("messages") or []
    replied = any(not m.get("from_candidate") for m in messages) or bool(raw.get("company_messaged_at"))
    status = (state.replace("_", " ").capitalize() or "Applied") + (" (company replied)" if replied else "")
    refs = raw.get("referenced_job_ids") or []
    first = next((m for m in messages if m.get("from_candidate")), None)
    return Application(
        id=str(raw["id"]),
        account=account,
        company=((raw.get("company") or {}).get("name") or "").strip(),
        job_id=str(refs[0]) if refs else "",
        title=(raw.get("applied_job_title") or job_title or "").strip(),
        status=status,
        state="inactive" if any(w in state for w in _CLOSED) else "active",
        applied_on=parse_date((first or {}).get("created_at")) if first else None,
    )


class WaasJob(Job):
    """A job from Work at a Startup search or a job page."""

    job_type: str | None = None
    role: str | None = None
    remote: str | None = None  # yes, only, no
    salary: str | None = None
    equity: str | None = None
    visa: str | None = None
    batch: str | None = None  # the company's YC batch, e.g. W26
    updated_on: str | None = None
    applied: bool = False


def to_job(job: dict[str, Any], company: dict[str, Any], applied_ids: set[str] | None = None) -> WaasJob:
    jid = str(job["id"])
    return WaasJob(
        id=jid,
        title=(job.get("title") or "").strip(),
        company=(company.get("name") or "").strip(),
        location=job.get("pretty_location_or_remote") or None,
        url=job.get("show_path") or f"{BASE_URL}/jobs/{jid}",
        description=(job.get("description") or "").strip()[:600] or None,
        job_type=job.get("pretty_job_type") or None,
        role=job.get("pretty_sub_type") or job.get("pretty_role") or None,
        remote=job.get("remote"),
        salary=job.get("pretty_salary_range") or None,
        equity=job.get("pretty_equity_range") or None,
        visa=job.get("pretty_sponsors_visa") or None,
        batch=company.get("batch"),
        updated_on=job.get("pretty_updated_at"),
        applied=jid in (applied_ids or set()),
    )


class WaasClient:
    def __init__(
        self,
        key: str,
        email: str,
        template: str,
        settings: WaasSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._key = key
        self._email = email
        self._template = template
        self._session_file = settings.session_path(key)
        self._csrf: str | None = None
        self._algolia: dict[str, str] | None = None
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=30.0,
            transport=transport,
            headers=browser_headers(),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "WaasClient":
        if cached := load_cached_session(self._session_file):
            for name, value in cached.items():
                self._http.cookies.set(name, value, domain=HOST)
        else:
            await self._login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        self._http.cookies.clear()
        self._csrf = self._algolia = None
        save_session(self._session_file, await login(self._http, self._email, self._template))

    async def _get(self, path: str, *, retry: bool = True) -> Any:
        """GET JSON. An expired session is bounced to the sign-in page (HTML) or refused outright."""
        resp = await self._http.get(path, headers={"accept": "application/json"})
        if resp.status_code in (401, 403) or "json" not in resp.headers.get("content-type", ""):
            if not retry:
                raise AuthError("Work at a Startup rejected a freshly created session.")
            await self._login()
            return await self._get(path, retry=False)
        if resp.status_code == 404:
            raise ValueError(f"Work at a Startup has no {path}.")
        resp.raise_for_status()
        return resp.json()

    async def _props(self, path: str) -> dict[str, Any]:
        return (await self._page(path))[0]

    async def _page(self, path: str, *, retry: bool = True) -> tuple[dict[str, Any], str]:
        """(Inertia props, HTML) of a signed-in page (`data-page`); logging in again if it isn't one."""
        resp = await self._http.get(path, headers={"accept": "text/html,application/xhtml+xml"})
        page: dict[str, Any] = {}
        if m := _PAGE.search(resp.text):
            try:
                page = json.loads(html.unescape(m.group(1)))
            except ValueError:
                page = {}
        props = page.get("props") or {}
        if resp.status_code in (401, 403) or not (props.get("navBar") or {}).get("loggedIn"):
            if resp.status_code == 404:
                raise ValueError(f"Work at a Startup has no page {path}.")
            if not retry:
                raise AuthError("Work at a Startup rejected a freshly created session.")
            await self._login()
            return await self._page(path, retry=False)
        self._csrf = csrf_token(resp.text) or self._csrf
        return props, resp.text

    async def job_page(self, job_id: str) -> dict[str, Any]:
        return await self._props(f"/jobs/{job_id}")

    async def job(self, job_id: str) -> WaasJob:
        props = await self.job_page(job_id)
        raw, company = props.get("job") or {}, props.get("companyFull") or props.get("company") or {}
        full = next((j for j in company.get("jobs") or [] if str(j.get("id")) == str(job_id)), None)
        if full:
            return to_job(full, company, {str(i) for i in props.get("appliedJobIds") or []})
        return WaasJob(
            id=str(job_id),
            title=raw.get("title") or "",
            company=(props.get("company") or {}).get("name") or "",
            location=raw.get("location"),
            url=f"{BASE_URL}/jobs/{job_id}",
            description=(raw.get("descriptionHtml") or "")[:600] or None,
            job_type=raw.get("jobType"),
            salary=raw.get("salaryRange"),
            visa=raw.get("sponsorsVisa"),
            applied=str(job_id) in {str(i) for i in props.get("appliedJobIds") or []},
        )

    async def conversation(self, conversation_id: str) -> dict[str, Any]:
        """The whole thread with a company (what both sides wrote)."""
        return await self._get(f"/api/conversations/{conversation_id}")

    async def applications(self) -> list[Application]:
        raw = await self._get("/api/conversations")
        gate = asyncio.Semaphore(4)

        async def title_of(r: dict[str, Any]) -> str:
            refs = r.get("referenced_job_ids") or []
            if r.get("applied_job_title") or not refs:
                return ""
            async with gate:
                try:
                    return str(((await self.job_page(str(refs[0]))).get("job") or {}).get("title") or "")
                except (ValueError, AuthError, httpx.HTTPError):
                    return ""  # the job may have been taken down

        titles = await asyncio.gather(*(title_of(r) for r in raw))
        apps = [to_application(r, self._key, t) for r, t in zip(raw, titles, strict=True)]
        save_session(self._session_file, cookies_for(self._http))
        return apps

    async def _search_keys(self) -> dict[str, str]:
        if self._algolia is None:
            _, text = await self._page("/companies")  # also checks the session and reads the CSRF token
            m = _ALGOLIA.search(text)
            try:
                opts = json.loads(m.group(1)) if m else {}
            except ValueError:
                opts = {}
            if not opts.get("app") or not opts.get("key"):
                raise AuthError("Work at a Startup's page had no search key; try again after logging in.")
            self._algolia = {"app": opts["app"], "key": opts["key"]}
        return self._algolia

    async def search_jobs(
        self,
        query: str = "",
        *,
        role: str | None = None,
        job_type: str | None = None,
        remote: str | None = None,
        page: int = 1,
    ) -> tuple[list[WaasJob], int]:
        """(jobs, total_pages). The site searches an Algolia index of jobs (with a per-session key from the
        page) that returns company ids, then loads those companies with all their jobs."""
        keys = await self._search_keys()
        parts = []
        if role:
            parts.append(f"(role:{role})")
        if remote:
            parts.append(f"(remote:{remote})")
        if job_type:
            parts.append(f'(job_type:"{job_type}")')
        params = urlencode(
            {
                "query": query,
                "page": page - 1,
                "filters": " AND ".join(parts),
                "attributesToRetrieve": json.dumps(["company_id"]),
                "attributesToHighlight": "[]",
                "attributesToSnippet": "[]",
                "hitsPerPage": 10,
                "distinct": "true",
            }
        )
        resp = await self._http.post(
            f"https://{keys['app'].lower()}-dsn.algolia.net/1/indexes/*/queries",
            params={"x-algolia-application-id": keys["app"], "x-algolia-api-key": keys["key"]},
            content=json.dumps({"requests": [{"indexName": INDEX, "params": params}]}),
            headers={"content-type": "application/x-www-form-urlencoded", "origin": BASE_URL},
        )
        if resp.status_code >= 400:
            self._algolia = None
            raise AuthError(f"Work at a Startup's search refused the request (HTTP {resp.status_code}).")
        result = resp.json()["results"][0]
        hits = result.get("hits") or []
        company_ids = list(dict.fromkeys(int(h["company_id"]) for h in hits))
        job_ids = [str(h["objectID"]) for h in hits]
        companies: list[dict[str, Any]] = []
        if company_ids:
            r = await self._http.post(
                "/companies/fetch",
                json={"ids": company_ids},
                headers={"accept": "application/json", "x-csrf-token": self._csrf or ""},
            )
            r.raise_for_status()
            companies = r.json().get("companies") or []
        by_job = {str(j["id"]): (j, c) for c in companies for j in c.get("jobs") or []}
        gate = asyncio.Semaphore(4)

        async def one(job_id: str) -> WaasJob | None:
            if job_id in by_job:
                return to_job(*by_job[job_id])
            async with gate:  # the company list leaves out some jobs the search found
                try:
                    return await self.job(job_id)
                except (ValueError, httpx.HTTPError):
                    return None

        found = await asyncio.gather(*(one(i) for i in job_ids))
        return [j for j in found if j], int(result.get("nbPages") or 1)
