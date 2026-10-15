"""MyGreenhouse candidate client. Logs in with an emailed code as needed."""

import asyncio
from datetime import date, datetime
from typing import Any

import httpx
from ats_core import Application, Job

from mcp_greenhouse.auth import (
    BASE_URL,
    HOST,
    XSRF_COOKIE,
    AuthError,
    browser_headers,
    cookies_for,
    inertia_page,
    load_cached_session,
    login,
    save_session,
)
from mcp_greenhouse.config import GreenhouseSettings


def parse_date(s: str | None) -> date | None:
    try:
        return datetime.fromisoformat(s or "").date()
    except ValueError:
        return None


def _label(entry: Any) -> str:
    """Display text for one stage or activity entry; the shapes beyond `{"type": "applied"}` are unseen."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("name", "title", "stage_name", "stage", "label", "type", "status"):
            if isinstance(entry.get(key), str) and entry[key].strip():
                return entry[key].strip().replace("_", " ").capitalize()
    return ""


def status_text(raw: dict[str, Any], detail: dict[str, Any] | None) -> str:
    """Active/Inactive for employers that don't share updates. For those that do (`sharingStatus`), the
    latest stage or activity from the application's detail view (e.g. "Applied"). An inactive application
    whose latest activity is just "Applied" reads "Inactive" (closed or rejected without a recorded stage)."""
    inactive = bool(raw.get("inactive"))
    base = "Inactive" if inactive else "Active"
    if not (raw.get("sharingStatus") and detail):
        return base
    latest = ""
    for source in (detail.get("activityHistory"), detail.get("stages")):
        if isinstance(source, list) and source:
            latest = _label(source[-1]) or latest
    if not latest or (inactive and latest.lower() == "applied"):
        return base
    return latest


def to_application(raw: dict[str, Any], account: str, detail: dict[str, Any] | None = None) -> Application:
    inactive = bool(raw.get("inactive"))
    return Application(
        id=str(raw["id"]),
        account=account,
        company=(raw.get("company_name") or "").strip(),
        job_id=str(raw.get("job_post_id") or ""),
        title=(raw.get("job_title") or "").strip(),
        status=status_text(raw, detail),
        state="inactive" if inactive else "active",
        applied_on=parse_date(raw.get("applied_at")),
    )


class GreenhouseJob(Job):
    """A job from MyGreenhouse recommendations or search."""

    work_type: str | None = None  # in_person, hybrid, remote
    pay: str | None = None
    posted_on: date | None = None
    viewed: bool = False
    applied_on: date | None = None  # set when the user already applied


def to_job(raw: dict[str, Any]) -> GreenhouseJob:
    path = raw.get("viewJobPath")
    desc = (raw.get("description") or raw.get("summary") or "").strip()
    return GreenhouseJob(
        id=str(raw["id"]),
        title=(raw.get("title") or "").strip(),
        company=(raw.get("companyName") or "").strip(),
        location="; ".join(raw.get("locations") or []) or None,
        url=BASE_URL + path if path else raw.get("publicUrl"),
        description=desc[:600] or None,
        work_type=raw.get("workType"),
        pay=raw.get("payRanges"),
        posted_on=parse_date(raw.get("firstPublished")),
        viewed=bool(raw.get("viewed")),
        applied_on=parse_date(raw.get("appliedAt")),
    )


class GreenhouseClient:
    def __init__(
        self,
        key: str,
        email: str,
        template: str,
        settings: GreenhouseSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._key = key
        self._version: str | None = None
        self._geocoder: dict[str, Any] = {}
        self._email = email
        self._template = template
        self._session_file = settings.session_path(key)
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=30.0,
            transport=transport,
            headers=browser_headers(),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "GreenhouseClient":
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
        save_session(self._session_file, await login(self._http, self._email, self._template))

    async def _get(
        self, path: str, params: dict[str, Any], *, retry: bool = True, accept: str = "*/*"
    ) -> Any:
        resp = await self._http.get(path, params=params, headers={"accept": accept})
        # An expired session is bounced to the sign-in page (HTML) or refused outright.
        if resp.status_code in (401, 403) or "json" not in resp.headers.get("content-type", ""):
            if not retry:
                raise AuthError("MyGreenhouse rejected a freshly created session.")
            await self._login()
            return await self._get(path, params, retry=False, accept=accept)
        resp.raise_for_status()
        return resp.json()

    async def recommendations(self) -> list[GreenhouseJob]:
        data = await self._get("/recommendations", {}, accept="application/json")
        return [to_job(j) for j in data]

    async def _inertia_version(self, refresh: bool = False, _retry: bool = True) -> str:
        """The site's asset version, which Inertia requires on its XHRs. Read from the HTML of a page that
        needs a login, so it doubles as the session check."""
        if self._version and not refresh:
            return self._version
        resp = await self._http.get("/jobs/search", headers={"accept": "text/html,application/xhtml+xml"})
        page = inertia_page(resp)
        if resp.status_code in (401, 403) or page.get("component") in (None, "login"):
            if not _retry:
                raise AuthError("MyGreenhouse rejected a freshly created session.")
            await self._login()
            return await self._inertia_version(refresh=True, _retry=False)
        self._version = str(page.get("version") or "")
        self._geocoder = (page.get("props") or {}).get("locationControlSettings") or {}
        return self._version

    async def resolve_location(self, text: str) -> dict[str, Any]:
        """Search params for a place name, as the site's location picker would send them. The picker asks
        a geocode.earth proxy (key and URL are in the page props) for coordinates; without them the search
        ignores the location."""
        await self._inertia_version()
        base, key = self._geocoder.get("baseUrl"), self._geocoder.get("apiKey")
        if not base or not key:
            raise AuthError("MyGreenhouse didn't offer a location service; search without a location.")
        resp = await self._http.get(
            base + "v1/autocomplete",
            params={"text": text, "api_key": key, "size": 1, "layers": "locality,region,country"},
            headers={"origin": BASE_URL},
        )
        resp.raise_for_status()
        features = resp.json().get("features") or []
        if not features:
            raise ValueError(f"MyGreenhouse couldn't find a place called {text!r}.")
        props = features[0]["properties"]
        lon, lat = features[0]["geometry"]["coordinates"]
        kind = {"locality": "city", "region": "region", "country": "country"}.get(
            props.get("layer"), "region"
        )
        names = [props.get(k) for k in ("locality", "region", "country") if props.get(k)]
        params: dict[str, Any] = {
            "location": ", ".join(dict.fromkeys(names)) or text,
            "lat": lat,
            "lon": lon,
            "location_type": kind,
            "country_short_name": props.get("country_code"),
        }
        if props.get("region_a"):
            params["state_short_name"] = props["region_a"]
        return {k: v for k, v in params.items() if v is not None}

    async def search_jobs(self, params: dict[str, Any], page: int = 1) -> tuple[list[GreenhouseJob], bool]:
        """(jobs, more_results_available) for one page of /jobs/search."""
        version = await self._inertia_version()
        query = {**params, **({"page": page} if page > 1 else {})}
        headers = {
            "accept": "text/html, application/xhtml+xml",
            "x-inertia": "true",
            "x-requested-with": "XMLHttpRequest",
            "x-inertia-partial-component": "jobs",
            "x-inertia-partial-data": "page,moreResultsAvailable,jobPosts",
        }
        resp = await self._http.get(
            "/jobs/search", params=query, headers={**headers, "x-inertia-version": version}
        )
        if resp.status_code == 409:  # the site was redeployed: our asset version is stale
            version = await self._inertia_version(refresh=True)
            resp = await self._http.get(
                "/jobs/search", params=query, headers={**headers, "x-inertia-version": version}
            )
        data = inertia_page(resp)
        props = data.get("props") or {}
        if resp.status_code >= 400 or data.get("component") != "jobs" or "jobPosts" not in props:
            raise AuthError(f"MyGreenhouse job search failed (HTTP {resp.status_code}).")
        return [to_job(j) for j in props["jobPosts"]], bool(props.get("moreResultsAvailable"))

    async def _pages(self, flag: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page, total = 1, 1
        while page <= total:
            data = await self._get("/applications.json", {"page": page, flag: "true"})
            block = data.get("inactive" if flag == "inactive_only" else "active") or {}
            out += block.get("applications", [])
            total = int(block.get("total_pages") or 1)
            page += 1
        return out

    async def _detail(self, application_id: str) -> dict[str, Any] | None:
        """The application's detail view, which has the activity history the list lacks."""
        version = await self._inertia_version()
        resp = await self._http.get(
            f"/applications/{application_id}",
            headers={
                "accept": "text/html, application/xhtml+xml",
                "x-inertia": "true",
                "x-inertia-version": version,
                "x-requested-with": "XMLHttpRequest",
                "x-inertia-partial-component": "applications",
                "x-inertia-partial-data": "selected_application_id,selected_application",
            },
        )
        props = inertia_page(resp).get("props") or {}
        detail = props.get("selected_application")
        return detail if resp.status_code == 200 and isinstance(detail, dict) else None

    async def applications(self) -> list[Application]:
        raw = await self._pages("active_only") + await self._pages("inactive_only")
        by_id = {str(r["id"]): r for r in raw}  # an id may show up twice
        gate = asyncio.Semaphore(4)

        async def detail_of(r: dict[str, Any]) -> dict[str, Any] | None:
            if not r.get("sharingStatus"):
                return None  # without sharing there is nothing beyond active/inactive to read
            async with gate:
                return await self._detail(str(r["id"]))

        details = await asyncio.gather(*(detail_of(r) for r in by_id.values()))
        apps = [to_application(r, self._key, d) for r, d in zip(by_id.values(), details, strict=True)]
        save_session(self._session_file, cookies_for(self._http))  # Rails may have rotated the cookie
        return apps

    async def mark_inactive(self, application_id: str, *, _retry: bool = True) -> None:
        """Move one application to MyGreenhouse's inactive list. This only changes the user's own tracker;
        the employer isn't told."""
        resp = await self._http.patch(
            f"/applications/{application_id}/mark_inactive",
            headers={
                "accept": "application/json, text/plain, */*",
                "x-csrf-token": self._http.cookies.get(XSRF_COOKIE) or "",
                "x-requested-with": "XMLHttpRequest",
                "origin": BASE_URL,
                "referer": f"{BASE_URL}/applications/{application_id}",
            },
        )
        if resp.status_code in (401, 403, 422) or "json" not in resp.headers.get("content-type", ""):
            if not _retry:
                raise AuthError("MyGreenhouse rejected the request with a freshly created session.")
            await self._login()
            return await self.mark_inactive(application_id, _retry=False)
        if resp.status_code == 404:
            raise ValueError(f"MyGreenhouse has no application {application_id} for this account.")
        resp.raise_for_status()
        if resp.json().get("success") is not True:
            raise AuthError(f"MyGreenhouse didn't confirm marking {application_id} inactive.")
        save_session(self._session_file, cookies_for(self._http))
