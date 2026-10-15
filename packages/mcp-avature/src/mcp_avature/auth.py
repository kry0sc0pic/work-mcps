"""Avature candidate login (email + password), driven by stored credentials."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from ats_core import Credential

from mcp_avature.accounts import AvatureAccount
from mcp_avature.parse import is_logged_in, is_login_page
from mcp_avature.waf import WafError, WafPass, challenged

DEFAULT_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


class AuthError(RuntimeError):
    pass


def browser_headers(agent: str = DEFAULT_AGENT) -> dict[str, str]:
    return {
        "user-agent": agent,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
    }


SolveWaf = Callable[[], Awaitable[None]]  # earns a fresh WAF token onto the client


@dataclass
class Session:
    cookies: dict[str, str]
    user_agent: str  # the WAF token is replayed with the user agent of the browser that earned it


async def send(
    http: httpx.AsyncClient, solve: SolveWaf, method: str, url: str, **kw: object
) -> httpx.Response:
    """One request; if the WAF answers with its challenge, earn a token and retry once."""
    for _ in range(2):
        resp = await http.request(method, url, **kw)  # type: ignore[arg-type]
        if not challenged(resp):
            return resp
        await solve()
    raise WafError("The site's bot check kept blocking the request.")


async def login(http: httpx.AsyncClient, solve: SolveWaf, account: AvatureAccount, cred: Credential) -> None:
    """Log in on `http` (which keeps cookies and follows redirects)."""
    url = f"{account.portal}/Login"
    page = await send(http, solve, "GET", url)
    if page.status_code != 200 or not is_login_page(page.text):
        raise AuthError(f"Could not load the Avature login page (HTTP {page.status_code}).")
    resp = await send(
        http,
        solve,
        "POST",
        url,
        data={"username": cred.username, "password": cred.password, "login": "Login"},
        headers={"origin": f"https://{account.host}", "referer": str(page.url)},
    )
    if not is_logged_in(resp.text):
        raise AuthError(
            f"Avature login failed for {account.key}. Check the saved credentials "
            "(SSO-only accounts can't use password login)."
        )


def cookies_for(http: httpx.AsyncClient, host: str) -> dict[str, str]:
    return {c.name: c.value or "" for c in http.cookies.jar if host.endswith((c.domain or "").lstrip("."))}


def load_cached_session(path: Path) -> Session | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cookies, agent = raw["cookies"], raw["user_agent"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return Session(cookies, agent) if cookies and isinstance(agent, str) else None


def save_session(path: Path, session: Session) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cookies": session.cookies, "user_agent": session.user_agent}), encoding="utf-8"
    )
    path.chmod(0o600)


def from_waf(waf: WafPass) -> Session:
    return Session(waf.cookies, waf.user_agent)
