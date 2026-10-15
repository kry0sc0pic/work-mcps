"""Work at a Startup login. The site delegates to YC's account service (account.ycombinator.com): you give
an email address, YC emails a 6-digit code, and a successful code ends in a redirect chain through
`authenticate_token` and `sso-auth.workatastartup.com/set_auth` that leaves session cookies on
www.workatastartup.com (`_bf_session_key`, `_sso.key`, `XSRF-TOKEN`). Those cookies are what we cache.
The code is read from the AgentMail inbox the user forwards YC's emails to (ats_core.mailbox).
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
from ats_core import mailbox

HOST = "www.workatastartup.com"
BASE_URL = f"https://{HOST}"
ACCOUNT_URL = "https://account.ycombinator.com"
SESSION_COOKIE = "_bf_session_key"
_CSRF = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


class AuthError(RuntimeError):
    pass


def browser_headers() -> dict[str, str]:
    return {
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "accept-language": "en-US,en;q=0.9",
        # The www host answers 406 to a bare */* on its pages.
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def csrf_token(html_text: str) -> str:
    m = _CSRF.search(html_text)
    return m.group(1) if m else ""


def _token_url(data: Any) -> str | None:
    """Where to go after the code was accepted: a redirect URL, or a token for `authenticate_token`."""
    if not isinstance(data, dict):
        return None
    for key in ("redirect_url", "redirect", "url", "location", "next"):
        if isinstance(data.get(key), str) and data[key].startswith("http"):
            return data[key]
    token = data.get("token") or data.get("auth_token")
    if isinstance(token, str) and token:
        return f"{ACCOUNT_URL}/authenticate_token"
    return None


async def login(http: httpx.AsyncClient, email: str, template: str) -> dict[str, str]:
    """Request a code for `email`, wait for it in the mailbox and submit it; return the session cookies.

    `http` keeps cookies and follows redirects. Can take up to the template's timeout (2 min)."""
    continue_url = BASE_URL + "/"
    page = await http.get(f"{ACCOUNT_URL}/", params={"continue": continue_url})
    token = csrf_token(page.text)
    if page.status_code != 200 or not token:
        raise AuthError(f"YC's sign-in page answered HTTP {page.status_code} without a CSRF token.")
    headers = {
        "accept": "application/json",
        "x-csrf-token": token,
        "x-requested-with": "XMLHttpRequest",
        "origin": ACCOUNT_URL,
        "referer": f"{ACCOUNT_URL}/?continue={continue_url}",
    }
    body = {"email": email, "continue": continue_url}
    resp = await http.post(f"{ACCOUNT_URL}/start_sign_in", headers=headers, json=body)
    if resp.status_code >= 400:
        raise AuthError(f"YC doesn't know the account {email!r} (HTTP {resp.status_code}).")
    since = mailbox.now()
    resp = await http.post(f"{ACCOUNT_URL}/login_link", headers=headers, json=body)
    if resp.status_code >= 400:
        raise AuthError(f"YC refused to send a login code (HTTP {resp.status_code}).")
    code = await asyncio.to_thread(mailbox.wait_for_code, template, since=since)
    resp = await http.post(f"{ACCOUNT_URL}/verify_otp", headers=headers, json={"otp_code": code})
    if resp.status_code >= 400:
        raise AuthError(f"YC rejected the login code (HTTP {resp.status_code}).")
    try:
        data = resp.json()
    except ValueError:
        data = None
    if (target := _token_url(data)) and isinstance(data, dict) and (tok := data.get("token")):
        await http.get(target, params={"continue": continue_url, "token": tok})
    elif target:
        await http.get(target)
    # With the session cookies in place the site answers as a signed-in user (the redirect chain above
    # normally did that already; the browser followed it by itself after verify_otp).
    home = await http.get(f"{BASE_URL}/companies", params={"jobType": "intern"})
    if home.status_code != 200 or '"loggedIn":true' not in home.text.replace("&quot;", '"').replace(" ", ""):
        raise AuthError("YC accepted the code but Work at a Startup didn't open a session.")
    return cookies_for(http)


def cookies_for(http: httpx.AsyncClient) -> dict[str, str]:
    """Cookies the www host needs: its own and the shared `.workatastartup.com` ones."""
    return {
        c.name: c.value or ""
        for c in http.cookies.jar
        if HOST.endswith((c.domain or "").lstrip(".")) and (c.domain or "").lstrip(".") != "ycombinator.com"
    }


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
