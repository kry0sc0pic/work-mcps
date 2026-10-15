"""MyGreenhouse (my.greenhouse.io) candidate login: email address, then an 8-digit code emailed to it.

The site is a Rails app with Inertia.js. Requests that mimic its own XHRs carry `x-inertia`, the page
version, and an `x-csrf-token` equal to the `MYGREENHOUSE-XSRF-TOKEN` cookie. The code is read from the
AgentMail inbox the user forwards Greenhouse's emails to (ats_core.mailbox). A login leaves `_session_id`
and the XSRF cookie in the jar; those are what we cache.
"""

import asyncio
import html
import json
import re
from pathlib import Path

import httpx
from ats_core import mailbox

HOST = "my.greenhouse.io"
BASE_URL = f"https://{HOST}"
SESSION_COOKIE = "_session_id"
XSRF_COOKIE = "MYGREENHOUSE-XSRF-TOKEN"
_PAGE = re.compile(r'data-page="([^"]*)"')


class AuthError(RuntimeError):
    pass


def browser_headers() -> dict[str, str]:
    return {
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "accept-language": "en-US,en;q=0.9",
    }


def inertia_headers(http: httpx.AsyncClient, version: str) -> dict[str, str]:
    headers = {
        "accept": "text/html, application/xhtml+xml",
        "x-inertia": "true",
        "x-inertia-version": version,
        "x-requested-with": "XMLHttpRequest",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/users/sign_in",
    }
    if token := http.cookies.get(XSRF_COOKIE):
        headers["x-csrf-token"] = token
    return headers


def inertia_page(resp: httpx.Response) -> dict:
    """The Inertia page props from either an HTML page (data-page attribute) or an Inertia JSON reply."""
    try:
        if m := _PAGE.search(resp.text):
            page = json.loads(html.unescape(m.group(1)))
        else:
            page = resp.json()
    except ValueError:
        return {}
    return page if isinstance(page, dict) else {}


async def login(http: httpx.AsyncClient, email: str, template: str) -> dict[str, str]:
    """Request a code for `email`, wait for it in the mailbox and submit it; return the session cookies.

    `http` keeps cookies and follows redirects. Can take up to the template's timeout (2 min)."""
    page = await http.get("/users/sign_in", headers={"accept": "text/html,application/xhtml+xml"})
    if page.status_code != 200:
        raise AuthError(f"{HOST} sign-in page answered HTTP {page.status_code}.")
    version = str(inertia_page(page).get("version") or "")
    since = mailbox.now()
    resp = await http.post(
        "/users/sign_in",
        headers=inertia_headers(http, version),
        json={"email": email, "job_board": None, "device_type": "desktop"},
    )
    if resp.status_code >= 400 or (inertia_page(resp).get("props") or {}).get("loginError"):
        raise AuthError(f"{HOST} refused the sign-in request (HTTP {resp.status_code}).")
    code = await asyncio.to_thread(mailbox.wait_for_code, template, since=since)
    resp = await http.post(
        "/users/submit_code",
        headers=inertia_headers(http, version),
        json={"code": code, "email": email, "time_zone": "UTC", "device_type": "desktop"},
    )
    props = inertia_page(resp).get("props") or {}
    if resp.status_code >= 400 or not props.get("currentUserId"):
        raise AuthError(f"{HOST} rejected the login code (HTTP {resp.status_code}).")
    return cookies_for(http)


def cookies_for(http: httpx.AsyncClient) -> dict[str, str]:
    return {c.name: c.value or "" for c in http.cookies.jar if HOST.endswith((c.domain or "").lstrip("."))}


def load_cached_session(path: Path) -> dict[str, str] | None:
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return cookies if isinstance(cookies, dict) and SESSION_COOKIE in cookies else None


def save_session(path: Path, cookies: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies), encoding="utf-8")
    path.chmod(0o600)
