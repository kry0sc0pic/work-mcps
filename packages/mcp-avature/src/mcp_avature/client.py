"""Avature candidate client. Logs in with stored credentials as needed."""

import re

import httpx
from ats_core import Application, Credential

from mcp_avature.accounts import AvatureAccount
from mcp_avature.auth import (
    AuthError,
    Session,
    browser_headers,
    cookies_for,
    from_waf,
    load_cached_session,
    login,
    save_session,
    send,
)
from mcp_avature.config import AvatureSettings
from mcp_avature.parse import is_logged_in, parse_applications, parse_schedule
from mcp_avature.waf import TokenProvider, browser_pass

# Only "Qualifications Under Review" has been seen, so which words mean the process has ended is a guess.
# A wrong guess only mislabels active/inactive; the status text is always kept.
_CLOSED = re.compile(
    r"not (?:been )?(?:selected|successful|considered|moving|progress)|unsuccessful|regret|"
    r"withdrawn|closed|rejected|declined|no longer|filled|cancel",
    re.I,
)


class AvatureClient:
    def __init__(
        self,
        account: AvatureAccount,
        cred: Credential,
        settings: AvatureSettings,
        transport: httpx.AsyncBaseTransport | None = None,
        token_provider: TokenProvider = browser_pass,
    ):
        self.account = account
        self._cred = cred
        self._session_file = settings.session_path(account.key)
        self._token_provider = token_provider
        self._http = httpx.AsyncClient(
            base_url=f"https://{account.host}",
            timeout=30.0,
            transport=transport,
            headers=browser_headers(),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "AvatureClient":
        if cached := load_cached_session(self._session_file):
            self._use(cached)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    def _use(self, session: Session) -> None:
        self._http.headers["user-agent"] = session.user_agent
        for name, value in session.cookies.items():
            self._http.cookies.set(name, value, domain=self.account.host)

    async def _solve_waf(self) -> None:
        """Earn an `aws-waf-token` in a headless browser and carry it on the plain client."""
        waf = await self._token_provider(f"https://{self.account.host}{self.account.portal}/Login")
        self._use(from_waf(waf))

    async def _login(self) -> None:
        # Drop the old session but keep the WAF token: it is what lets requests reach the site at all.
        for name in cookies_for(self._http, self.account.host):
            if name != "aws-waf-token":
                self._http.cookies.delete(name)
        await login(self._http, self._solve_waf, self.account, self._cred)
        self._save()

    def _save(self) -> None:
        agent = self._http.headers["user-agent"]
        save_session(self._session_file, Session(cookies_for(self._http, self.account.host), agent))

    async def _get(self, path: str, *, _retry: bool = True) -> str:
        resp = await send(self._http, self._solve_waf, "GET", self.account.portal + path)
        # An expired session shows the login page (status 200), so check for the Logout link.
        if resp.status_code in (401, 403) or not is_logged_in(resp.text):
            if not _retry:
                raise AuthError("Avature rejected a freshly created session.")
            await self._login()
            return await self._get(path, _retry=False)
        resp.raise_for_status()
        return resp.text

    async def schedule(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """(registered events, scheduled interviews) from the profile page."""
        result = parse_schedule(await self._get("/Profile"))
        self._save()
        return result

    async def applications(self) -> list[Application]:
        listed = parse_applications(await self._get("/Profile"))
        self._save()
        return [
            Application(
                id=a.job_id,
                account=self.account.key,
                job_id=a.job_id,
                title=a.title,
                status=a.status,
                state="inactive" if _CLOSED.search(a.status) else "active",
                applied_on=a.applied_on,
            )
            for a in listed
        ]
