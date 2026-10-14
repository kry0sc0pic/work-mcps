"""Oracle Recruiting Cloud candidate login: email address, then a one-time code emailed to it.

The code is read from the AgentMail inbox the user forwards the site's emails to (ats_core.mailbox).
A successful login yields an access code that later requests send as a header; there are no cookies.
"""

import asyncio
import json
import uuid
from pathlib import Path

import httpx
from ats_core import mailbox
from pydantic import BaseModel

from mcp_oracle.accounts import OracleAccount

API = "/hcmRestApi/resources/latest"
JSON_TYPE = "application/vnd.oracle.adf.resourceitem+json;charset=utf-8"


class AuthError(RuntimeError):
    pass


class Session(BaseModel):
    user_id: str  # a client-generated UUID, sent as ora-irc-cx-userid
    access_code: str


def base_headers(account: OracleAccount) -> dict[str, str]:
    origin = f"https://{account.host}"
    return {
        "accept": "*/*",
        "content-type": JSON_TYPE,
        "ora-irc-language": "en",
        "origin": origin,
        "referer": origin + "/",
    }


def session_headers(session: Session) -> dict[str, str]:
    return {"ora-irc-cx-userid": session.user_id, "ora-irc-access-code": session.access_code}


async def login(http: httpx.AsyncClient, account: OracleAccount, email: str, template: str) -> Session:
    """Request a code for `email`, wait for it in the mailbox, and exchange it for a session.

    `http` must have base_url https://{api_host}. Can take up to the template's timeout (2 min)."""
    user_id = str(uuid.uuid4())
    since = mailbox.now()
    resp = await http.post(
        f"{API}/recruitingCEVerificationTokens",
        headers={"ora-irc-cx-userid": user_id, "ora-irc-rest-action": "CSS_ACCESS:EMAIL"},
        json={
            "SiteNumber": account.site,
            "Mode": "CSS_ACCESS",
            "CaptchaToken": "",
            "Control": "",
            "Email": email,
        },
    )
    body = _json(resp)
    if resp.status_code >= 300:
        raise AuthError(f"{account.host} refused the sign-in request (HTTP {resp.status_code}).")
    if not body.get("AccessCode"):  # normally a code is emailed instead
        if not body.get("ChallengeFlag"):
            raise AuthError(f"{account.host} didn't send a login code; is {email} registered there?")
        code = await asyncio.to_thread(mailbox.wait_for_code, template, since=since)
        resp = await http.post(
            f"{API}/recruitingCEVerificationTokens",
            headers={"ora-irc-cx-userid": user_id},
            json={"ShortCode": code, "ChallengeFlag": True, "Email": email},
        )
        body = _json(resp)
    if resp.status_code >= 300 or not body.get("AccessCode"):
        raise AuthError(f"{account.host} rejected the login code (HTTP {resp.status_code}).")
    return Session(user_id=user_id, access_code=body["AccessCode"])


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load_session(path: Path) -> Session | None:
    try:
        return Session.model_validate_json(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def save_session(path: Path, session: Session) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session.model_dump()), encoding="utf-8")
    path.chmod(0o600)
