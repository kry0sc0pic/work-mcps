"""Workday candidate-experience (CXS) client. Logs in with stored credentials as needed."""

from datetime import date, datetime
from typing import Any, Literal

import httpx
from ats_core import Application, Credential

from mcp_workday.accounts import WorkdayAccount
from mcp_workday.auth import (
    CSRF_COOKIE,
    SessionExpiredError,
    load_cached_session,
    login,
    save_session,
)
from mcp_workday.config import WorkdaySettings

PAGE_SIZE = 8  # the server returns 400 for limit > 8


def parse_date(s: str | None) -> date | None:
    # The API returns both "28 August 2026" and "August 28, 2026" depending on the call.
    for fmt in ("%d %B %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s or "", fmt).date()
        except ValueError:
            continue
    return None


def to_application(raw: dict[str, Any], state: Literal["active", "inactive"], account: str) -> Application:
    return Application(
        id=raw["applicationId"],
        account=account,
        job_id=raw.get("jobRequisitionId", ""),
        title=(raw.get("postingTitle") or "").strip(),
        status=raw.get("status", ""),
        state=state,
        applied_on=parse_date(raw.get("dateApplied")),
        can_withdraw=raw.get("canWithdraw", False),
        is_draft=raw.get("draft", False),
        has_pending_tasks=raw.get("hasAwaitingTasks", False),
    )


class WorkdayClient:
    def __init__(
        self,
        account: WorkdayAccount,
        cred: Credential,
        settings: WorkdaySettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.account = account
        self._cred = cred
        self._session_file = settings.session_path(account.key)
        self._base = f"/wday/cxs/{account.tenant}/{account.site}"
        self._cookies: dict[str, str] = {}
        self._http = httpx.AsyncClient(
            base_url=f"https://{account.host}",
            timeout=30.0,
            transport=transport,
            headers={"accept": "application/json, text/html"},
        )

    async def __aenter__(self) -> "WorkdayClient":
        cached = load_cached_session(self._session_file)
        if cached:
            self._use(cached)
        else:
            await self._login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    def _use(self, cookies: dict[str, str]) -> None:
        self._cookies = cookies
        self._http.cookies.clear()
        for k, v in cookies.items():
            self._http.cookies.set(k, v, domain=self.account.host)

    async def _login(self) -> None:
        self._http.cookies.clear()
        cookies = await login(self._http, self.account, self._cred)
        save_session(self._session_file, cookies)
        self._use(cookies)

    async def _get(self, path: str, *, _retry: bool = True, **params: Any) -> Any:
        # Reads work without it, but send the CSRF token when the server issued one.
        csrf = self._cookies.get(CSRF_COOKIE)
        resp = await self._http.get(
            self._base + path, params=params, headers={"x-calypso-csrf-token": csrf} if csrf else {}
        )
        is_json = "json" in resp.headers.get("content-type", "")
        if resp.status_code in (301, 302, 303, 307, 401, 403) or not is_json:
            if not _retry:
                raise SessionExpiredError("Workday rejected a freshly created session.")
            await self._login()
            return await self._get(path, _retry=False, **params)
        resp.raise_for_status()
        return resp.json()

    async def _applications(self, state: Literal["active", "inactive"]) -> list[Application]:
        out: list[Application] = []
        offset = 0
        while True:
            page = await self._get("/applications", type=state, offset=offset, limit=PAGE_SIZE)
            out += [to_application(a, state, self.account.key) for a in page["data"]]
            offset += PAGE_SIZE
            if offset >= page["total"] or not page["data"]:
                return out

    async def applications(self) -> list[Application]:
        return await self._applications("active") + await self._applications("inactive")
