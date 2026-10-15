"""TAL.net candidate client. Logs in with stored credentials as needed."""

import asyncio
import re

import httpx
from ats_core import Application, Credential
from pydantic import BaseModel

from mcp_tal.accounts import TalAccount
from mcp_tal.auth import (
    AuthError,
    browser_headers,
    cookies_for,
    load_cached_session,
    login,
    pass_check,
    save_session,
)
from mcp_tal.config import TalSettings
from mcp_tal.parse import (
    Email,
    Message,
    is_logged_in,
    main_text,
    parse_application,
    parse_application_list,
    parse_correspondence,
    parse_email,
)

# The only status text seen is "Application Update" with "currently under review", so which words mean
# the process has ended is a guess. A wrong guess only mislabels active/inactive; the text is always kept.
_CLOSED = re.compile(
    r"unsuccessful|not (?:been )?successful|unfortunately|not (?:be )?(?:progress|proceed|move)|"
    r"regret|withdrawn|closed|rejected|declined|no longer",
    re.I,
)


def status_text(heading: str, message: str) -> str:
    """The list only says "Application Update" for everything, so add the employer's own message (its first
    paragraph) to let a change in wording show up as a status change."""
    first = next((ln for ln in message.split("\n") if ln.strip()), "")
    text = f"{heading}: {first}" if heading and first else heading or first
    return text[:300]


class TalApplicationDetail(BaseModel):
    id: str
    title: str
    status: str
    message: str
    communications: list[Message]
    details: str  # the "Review Event/Program Details" page as text


class TalClient:
    def __init__(
        self,
        account: TalAccount,
        cred: Credential,
        settings: TalSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.account = account
        self._cred = cred
        self._session_file = settings.session_path(account.key)
        self._http = httpx.AsyncClient(
            base_url=f"https://{account.host}",
            timeout=30.0,
            transport=transport,
            headers=browser_headers(),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "TalClient":
        if cached := load_cached_session(self._session_file):
            for name, value in cached.items():
                self._http.cookies.set(name, value, domain=self.account.host)
        else:
            await self._login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        self._http.cookies.clear()
        save_session(self._session_file, await login(self._http, self.account, self._cred))

    async def _get(self, path: str, *, _retry: bool = True) -> str:
        resp = await self._http.get(path)
        if not is_logged_in(resp.text):
            resp = await pass_check(self._http, resp) if "altcha-widget" in resp.text else resp
        # An expired session shows the signed-out page (status 200), so check for the user menu.
        if resp.status_code in (401, 403) or not is_logged_in(resp.text):
            if not _retry:
                raise AuthError("TAL.net rejected a freshly created session.")
            await self._login()
            return await self._get(path, _retry=False)
        if resp.status_code == 404:
            raise ValueError(f"TAL.net has no page {path} for this account.")
        resp.raise_for_status()
        return resp.text

    async def applications(self) -> list[Application]:
        listed = parse_application_list(await self._get("/candidate/application"))
        gate = asyncio.Semaphore(4)

        async def one(app_id: str) -> str:
            async with gate:
                page = parse_application(await self._get(f"/candidate/application/{app_id}"))
            return status_text(page.heading, page.message)

        statuses = await asyncio.gather(*(one(a.id) for a in listed))
        apps = [
            Application(
                id=a.id,
                account=self.account.key,
                job_id=a.id,
                title=a.title,
                status=status or a.status,
                state="inactive" if _CLOSED.search(status or a.status) else "active",
            )
            for a, status in zip(listed, statuses, strict=True)
        ]
        save_session(self._session_file, cookies_for(self._http, self.account.host))
        return apps

    async def application(self, application_id: str) -> TalApplicationDetail:
        base = f"/candidate/application/{_digits(application_id)}"
        page = parse_application(await self._get(base))
        comms = parse_correspondence(await self._get(base + "/correspondence"))
        details = main_text(await self._get(base + "/opportunity"))
        return TalApplicationDetail(
            id=_digits(application_id),
            title=page.title,
            status=status_text(page.heading, page.message),
            message=page.message,
            communications=comms,
            details=details,
        )

    async def email(self, application_id: str, message_id: str) -> Email:
        path = f"/candidate/application/{_digits(application_id)}/correspondence/email/{_digits(message_id)}"
        return parse_email(await self._get(path))


def _digits(value: str) -> str:
    """Ids go into URL paths; refuse anything but digits."""
    if not value.isdigit():
        raise ValueError(f"Not a TAL.net id: {value!r}")
    return value
