"""Programmatic Workday candidate login (username/password), driven by stored credentials."""

import json
import re
import secrets
from pathlib import Path

import httpx
from ats_core import Credential

from mcp_workday.accounts import WorkdayAccount

_TOKEN_RE = re.compile(r'"sessionSecureToken"\s*:\s*"([^"]+)"')
SESSION_COOKIE = "CALYPSO_SESSION"
CSRF_COOKIE = "CALYPSO_CSRF_TOKEN"


class AuthError(RuntimeError):
    pass


class SessionExpiredError(AuthError):
    pass


async def login(http: httpx.AsyncClient, account: WorkdayAccount, cred: Credential) -> dict[str, str]:
    """Log in on `http` (which keeps cookies) and return the resulting cookies for the host."""
    base = f"/en-US/{account.site}"
    page = await http.get(f"{base}/login")
    token = _TOKEN_RE.search(page.text)
    if page.status_code != 200 or not token:
        raise AuthError(f"Could not load the Workday login page (HTTP {page.status_code}).")
    home = f"https://{account.host}{base}/userHome"
    resp = await http.post(
        f"{base}/login",
        params={"redirect": f"{base}/userHome"},
        data={
            "username": cred.username,
            "password": cred.password,
            "successRedirectUri": home,
            "sessionSecureToken": token.group(1),
            "clientRequestID": secrets.token_hex(6),
        },
        headers={
            "session-secure-token": token.group(1),
            "origin": f"https://{account.host}",
            "referer": f"https://{account.host}{base}/login",
        },
    )
    cookies = cookies_for(http, account.host)
    if resp.status_code not in (302, 303) or SESSION_COOKIE not in cookies:
        raise AuthError(
            f"Workday login failed for {account.key} (HTTP {resp.status_code}): {_server_message(resp)} "
            "Check the saved credentials; SSO-only accounts can't use password login."
        )
    return cookies


def _server_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", "")).strip()
    except ValueError:
        return ""


def cookies_for(http: httpx.AsyncClient, host: str) -> dict[str, str]:
    return {c.name: c.value or "" for c in http.cookies.jar if host.endswith((c.domain or "").lstrip("."))}


def load_cached_session(path: Path) -> dict[str, str] | None:
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return cookies if SESSION_COOKIE in cookies else None


def save_session(path: Path, cookies: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies), encoding="utf-8")
    path.chmod(0o600)
