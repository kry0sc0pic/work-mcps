"""Yello candidate client. Logs in with stored credentials as needed."""

import re

import httpx
from ats_core import Application, Credential

from mcp_yello.accounts import YelloAccount
from mcp_yello.auth import (
    AuthError,
    browser_headers,
    cookies_for,
    load_cached_session,
    login,
    save_session,
)
from mcp_yello.config import YelloSettings
from mcp_yello.parse import is_logged_in, parse_applications, parse_schedule

# Only "Application Submitted" has been seen, so which words mean the process has ended is a guess. A wrong
# guess only mislabels active/inactive; the status text is always kept.
_CLOSED = re.compile(
    r"unsuccessful|not (?:been )?(?:successful|selected)|unfortunately|regret|withdrawn|closed|rejected|"
    r"declined|no longer",
    re.I,
)


class YelloClient:
    def __init__(
        self,
        account: YelloAccount,
        cred: Credential,
        settings: YelloSettings,
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

    async def __aenter__(self) -> "YelloClient":
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

    async def _home(self, *, _retry: bool = True) -> str:
        resp = await self._http.get("/")
        # An expired session redirects to the CAS sign-in page, so check the page itself.
        if resp.status_code in (401, 403) or not is_logged_in(resp.text):
            if not _retry:
                raise AuthError("Yello rejected a freshly created session.")
            await self._login()
            return await self._home(_retry=False)
        resp.raise_for_status()
        save_session(self._session_file, cookies_for(self._http, self.account.host))
        return resp.text

    async def applications(self) -> list[Application]:
        return [
            Application(
                id=a.id,
                account=self.account.key,
                job_id=a.requisition_id or a.id,
                title=a.title,
                status=a.status,
                state="inactive" if _CLOSED.search(a.status) else "active",
            )
            for a in parse_applications(await self._home())
        ]

    async def schedule(self) -> dict[str, list[str]]:
        return parse_schedule(await self._home())
