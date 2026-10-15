"""Programmatic TAL.net candidate login (email + password), driven by stored credentials."""

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from ats_core import Credential

from mcp_tal.accounts import TalAccount
from mcp_tal.parse import hidden_form, hidden_input, is_logged_in, needs_check

SESSION_COOKIES = (
    "wcn_session",
)  # a "bypassCaptcha" cookie from the Quick Check page is needed too, if issued
LOGIN_PATH = "/candidate/login"


class AuthError(RuntimeError):
    pass


def browser_headers() -> dict[str, str]:
    return {
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-GB,en;q=0.9",
    }


def _solve(challenge: dict[str, Any]) -> int:
    """altcha proof of work: the number n whose SHA-512 of salt+n equals the challenge (n <= maxnumber)."""
    salt, target = challenge["salt"], challenge["challenge"]
    for n in range(int(challenge["maxnumber"]) + 1):
        if hashlib.sha512(f"{salt}{n}".encode()).hexdigest() == target:
            return n
    raise AuthError("Couldn't solve the site's 'Quick Check' challenge.")


async def pass_check(http: httpx.AsyncClient, page: httpx.Response) -> httpx.Response:
    """Some visits get a 'Quick Check Needed' page (altcha) before the real one. Solve it the way the
    browser widget does and post the answer back to the same URL; the reply is the page we asked for."""
    for _ in range(2):
        if not needs_check(page.text):
            return page
        url = str(page.url).split("?")[0]
        ch = await http.get(url, params={"oleeoProtectCaptcha": "1"}, headers={"accept": "application/json"})
        try:
            challenge = ch.json()
            number = await asyncio.to_thread(_solve, challenge)
        except (ValueError, KeyError) as e:
            raise AuthError("The site's 'Quick Check' challenge had an unexpected shape.") from e
        answer = {
            "algorithm": challenge["algorithm"],
            "challenge": challenge["challenge"],
            "number": number,
            "salt": challenge["salt"],
            "signature": challenge["signature"],
            "took": 1,
        }
        page = await http.post(
            url,
            data={
                "altcha": base64.b64encode(json.dumps(answer, separators=(",", ":")).encode()).decode(),
                "originalRequestToken": hidden_input(page.text, "originalRequestToken") or "",
            },
            headers={"origin": str(http.base_url).rstrip("/"), "referer": str(page.url)},
        )
    if needs_check(page.text):
        raise AuthError("The site kept asking for its 'Quick Check' after it was answered.")
    return page


@dataclass
class Session:
    cookies: dict[str, str]
    # The `/vx/<lang>/<brand>/user-<n>/xf-<token>` path the login redirect lands on. After a scripted login
    # only URLs under it count as signed in; the bare `/candidate/...` ones show the signed-out page.
    base: str


async def login(http: httpx.AsyncClient, account: TalAccount, cred: Credential) -> Session:
    """Log in on `http` (which keeps cookies and follows redirects) and return the session."""
    page = await pass_check(http, await http.get(LOGIN_PATH))
    action, fields = hidden_form(page.text, "login_form")
    if page.status_code != 200 or not action:
        raise AuthError(f"Could not load the TAL.net login page (HTTP {page.status_code}).")
    resp = await http.post(
        action,
        data={**fields, "user": cred.username, "password": cred.password, "form_submit": "Login"},
        headers={"origin": f"https://{account.host}", "referer": str(page.url)},
    )
    cookies = cookies_for(http, account.host)
    if not is_logged_in(resp.text) or "wcn_session" not in cookies:
        raise AuthError(
            f"TAL.net login failed for {account.key}. Check the saved credentials "
            "(the password is case sensitive; SSO-only accounts can't use password login)."
        )
    return Session(cookies, urlsplit(str(resp.url)).path.split("/candidate")[0])


def cookies_for(http: httpx.AsyncClient, host: str) -> dict[str, str]:
    return {c.name: c.value or "" for c in http.cookies.jar if host.endswith((c.domain or "").lstrip("."))}


def load_cached_session(path: Path) -> Session | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cookies, base = raw["cookies"], raw["base"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return Session(cookies, base) if all(n in cookies for n in SESSION_COOKIES) else None


def save_session(path: Path, session: Session) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": session.cookies, "base": session.base}), encoding="utf-8")
    path.chmod(0o600)
