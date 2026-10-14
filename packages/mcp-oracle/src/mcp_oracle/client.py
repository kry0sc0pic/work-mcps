"""Oracle Recruiting Cloud candidate-experience client. Logs in with an emailed code as needed."""

from datetime import date, datetime
from typing import Any

import httpx
from ats_core import Application

from mcp_oracle.accounts import OracleAccount
from mcp_oracle.auth import (
    API,
    AuthError,
    Session,
    base_headers,
    load_session,
    login,
    save_session,
    session_headers,
)
from mcp_oracle.config import OracleSettings

PAGE_SIZE = 499  # what the site's own UI requests
_EXPAND = (
    "secondarySubmissions,ScheduleInterviews,UnscheduledInterviewRequests,"
    "BackgroundCheckLinks,assessmentLinks"
)


def parse_date(s: str | None) -> date | None:
    try:
        return datetime.fromisoformat(s or "").date()
    except ValueError:
        return None


def to_application(raw: dict[str, Any], account: str) -> Application:
    pending = bool(
        raw.get("AssessmentsFlag")
        or raw.get("assessmentLinks")
        or raw.get("BackgroundCheckLinks")
        or raw.get("UnscheduledInterviewRequests")
    )
    return Application(
        id=str(raw["SubmissionId"]),
        account=account,
        job_id=str(raw.get("RequisitionNumber") or ""),
        title=(raw.get("JobTitle") or "").strip(),
        status=raw.get("Status") or "",
        state="active" if raw.get("ActiveFlag") else "inactive",
        applied_on=parse_date(raw.get("SubmissionDate")),
        has_pending_tasks=pending,
    )


def draft_to_application(raw: dict[str, Any], account: str) -> Application:
    # Drafts carry no title or status of their own.
    req = str(raw.get("RequisitionNumber") or "")
    return Application(
        id=f"draft-{raw.get('APPDraftId')}",
        account=account,
        job_id=req,
        title=f"Draft application (requisition {req})" if req else "Draft application",
        status="Draft",
        state="active",
        applied_on=parse_date(raw.get("ModificationDate")),
        is_draft=True,
    )


class OracleClient:
    def __init__(
        self,
        account: OracleAccount,
        email: str,
        template: str,
        settings: OracleSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.account = account
        self._email = email
        self._template = template
        self._session_file = settings.session_path(account.key)
        self._session: Session | None = None
        self._http = httpx.AsyncClient(
            base_url=f"https://{account.api_host}",
            timeout=30.0,
            transport=transport,
            headers=base_headers(account),
        )

    async def __aenter__(self) -> "OracleClient":
        self._session = load_session(self._session_file)
        if self._session is None:
            await self._login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _login(self) -> None:
        self._session = await login(self._http, self.account, self._email, self._template)
        save_session(self._session_file, self._session)

    async def _get(
        self, path: str, *, _retry: bool = True, _headers: dict[str, str] | None = None, **params: Any
    ) -> Any:
        assert self._session is not None
        resp = await self._http.get(
            API + path, params=params, headers={**session_headers(self._session), **(_headers or {})}
        )
        if resp.status_code in (401, 403) or "json" not in resp.headers.get("content-type", ""):
            if not _retry:
                raise AuthError("The careers site rejected a freshly created session.")
            await self._login()
            return await self._get(path, _retry=False, _headers=_headers, **params)
        resp.raise_for_status()
        return resp.json()

    async def _items(
        self, path: str, _headers: dict[str, str] | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._get(
                path, _headers=_headers, limit=PAGE_SIZE, offset=offset, onlyData="true", **params
            )
            out += page.get("items", [])
            offset += PAGE_SIZE
            if not page.get("hasMore") or not page.get("items"):
                return out

    async def applications(self) -> list[Application]:
        key = self.account.key
        submitted = await self._items(
            "/recruitingCEJobApplications", expand=_EXPAND, orderBy="SubmissionDate:desc"
        )
        drafts = await self._items(
            "/recruitingCEJobApplicationDrafts",
            _headers={"ora-irc-rest-action": "CE_JA_DRAFT"},  # the endpoint answers 403 without it
            fields="RequisitionNumber,APPDraftId,SiteNumber,ModificationDate,SubmissionId",
            orderBy="ModificationDate:desc",
        )
        return [to_application(a, key) for a in submitted] + [
            draft_to_application(d, key) for d in drafts if not d.get("SubmissionId")
        ]
