"""Yello candidate login: a CAS single sign-on form (email + password), driven by stored credentials."""

import json
from pathlib import Path
from urllib.parse import urljoin

import httpx
from ats_core import Credential

from mcp_yello.accounts import YelloAccount
from mcp_yello.parse import hidden_form, is_logged_in

SESSION_COOKIE = "_session_id"


class AuthError(RuntimeError):
    pass


def browser_headers() -> dict[str, str]:
    return {
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-GB,en;q=0.9",
    }


async def login(http: httpx.AsyncClient, account: YelloAccount, cred: Credential) -> dict[str, str]:
    """Log in on `http` (which keeps cookies and follows redirects) and return the portal's cookies.

    GET /login redirects to the shared CAS host with the portal as `service`. The form posts `username`,
    `password`, the one-time `execution` token and `_eventId=submit`; success redirects back through a
    service ticket to the portal home. A failed login re-renders the form."""
    page = await http.get("/login")
    action, fields = hidden_form(page.text)
    if page.status_code != 200 or not fields.get("execution"):
        raise AuthError(f"Could not load the Yello sign-in page (HTTP {page.status_code}).")
    # The form's action is a bare "login": resolving it drops `?service=<portal>`, and without that the CAS
    # server answers "Authentication attempt has failed" even for correct credentials.
    target = urljoin(str(page.url), action or str(page.url))
    if "?" not in target and page.url.query:
        target += "?" + page.url.query.decode()
    resp = await http.post(
        target,
        data={**fields, "username": cred.username, "password": cred.password},
        headers={"origin": f"{page.url.scheme}://{page.url.host}", "referer": str(page.url)},
    )
    cookies = cookies_for(http, account.host)
    if not is_logged_in(resp.text) or SESSION_COOKIE not in cookies:
        raise AuthError(
            f"Yello login failed for {account.key}. Check the saved credentials "
            "(the password is case sensitive)."
        )
    return cookies


def cookies_for(http: httpx.AsyncClient, host: str) -> dict[str, str]:
    return {c.name: c.value or "" for c in http.cookies.jar if (c.domain or "").lstrip(".") == host}


def load_cached_session(path: Path) -> dict[str, str] | None:
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))["cookies"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return cookies if isinstance(cookies, dict) and SESSION_COOKIE in cookies else None


def save_session(path: Path, cookies: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")
    path.chmod(0o600)
