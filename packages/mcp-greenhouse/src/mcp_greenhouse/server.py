import asyncio
import functools
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any, Literal

from ats_core import (
    Application,
    ApplicationCache,
    ATSClient,
    CredentialStore,
    Delta,
    Job,
    SnapshotStore,
    StatusEvent,
    diff,
    is_fresh,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from mcp_greenhouse.auth import AuthError
from mcp_greenhouse.client import GreenhouseClient, GreenhouseJob
from mcp_greenhouse.config import GreenhouseSettings

mcp = MCPServer("greenhouse")

# Public Job Board API (no auth).
BOARD_API = "https://boards-api.greenhouse.io/v1/boards"
MAIL_TEMPLATE = "greenhouse"
ACCOUNT = "my.greenhouse.io"  # one login per site, like the other platforms; the email is its username


def user_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """mcp 2.x hides ordinary exceptions as crashes; only ToolError messages reach the model.
    Expected failures (bad input, unknown account, no mailbox, wrong code) must say what went wrong."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (
            ValueError,
            AuthError,
            RuntimeError,
        ) as e:  # MailboxError and CredentialStoreError are RuntimeErrors
            raise ToolError(str(e)) from e

    return wrapper


def credential_store(settings: GreenhouseSettings) -> CredentialStore:
    return CredentialStore("greenhouse", settings.config_dir)


def select_accounts(settings: GreenhouseSettings, account: str | None = None) -> list[str]:
    """Which saved accounts a request is about: one, or all."""
    saved = credential_store(settings).list()
    if not saved:
        raise ValueError("No Greenhouse accounts saved. Use greenhouse_add_account first.")
    if account is not None:
        if account not in saved:
            raise ValueError(f"Unknown account {account!r}. Saved: {', '.join(sorted(saved))}")
        return [account]
    return sorted(saved)


def filter_applications(
    apps: list[Application],
    *,
    company: str | None = None,
    state: Literal["active", "inactive", "all"] = "all",
    status: str | None = None,
    title: str | None = None,
    applied_after: date | None = None,
) -> list[Application]:
    out = apps
    if company:
        out = [a for a in out if company.lower() in a.company.lower()]
    if state != "all":
        out = [a for a in out if a.state == state]
    if status:
        out = [a for a in out if status.lower() in a.status.lower()]
    if title:
        out = [a for a in out if title.lower() in a.title.lower()]
    if applied_after:
        out = [a for a in out if a.applied_on and a.applied_on >= applied_after]
    return sorted(out, key=lambda a: a.applied_on or date.min, reverse=True)


@mcp.tool()
async def greenhouse_list_jobs(board_token: str) -> list[Job]:
    """List open jobs on a company's public Greenhouse job board (no login)."""
    client = ATSClient(BOARD_API)
    try:
        data = await client.get(f"/{board_token}/jobs")
    finally:
        await client.aclose()
    return [
        Job(
            id=str(j["id"]),
            title=j["title"],
            company=board_token,
            location=(j.get("location") or {}).get("name"),
            url=j.get("absolute_url"),
        )
        for j in data["jobs"]
    ]


@mcp.tool()
@user_errors
async def greenhouse_add_account(email: str) -> dict[str, str]:
    """Add a MyGreenhouse (my.greenhouse.io) account. Nothing secret is stored.

    MyGreenhouse logs in with an email address and an 8-digit code emailed to it. The code is read
    from the user's AgentMail inbox (set up with `work-mcp mailbox`), so the user must forward
    Greenhouse's emails there. The first fetch triggers a code email and can take up to two minutes.

    Args:
        email: the address the MyGreenhouse account uses.
    """
    key = email.strip().lower()
    if "@" not in key:
        raise ValueError(f"{email!r} isn't an email address.")
    meta = {"company": "Greenhouse", "auth": "email-code", "mail_template": MAIL_TEMPLATE, "source": "typed"}
    credential_store(GreenhouseSettings()).save_account(ACCOUNT, key, meta)
    return {"account": ACCOUNT, "email": key}


@mcp.tool()
@user_errors
async def greenhouse_list_accounts() -> dict[str, dict[str, str]]:
    """List the MyGreenhouse accounts the user has registered.

    There is one account, `my.greenhouse.io`, whose username is the login email. It covers every
    employer that uses Greenhouse.
    """
    return credential_store(GreenhouseSettings()).list()


@mcp.tool()
@user_errors
async def greenhouse_delete_account(account: str = ACCOUNT) -> bool:
    """Forget the account and its cached session and data."""
    settings = GreenhouseSettings()
    key = account
    settings.session_path(key).unlink(missing_ok=True)
    settings.snapshot_path(key).unlink(missing_ok=True)
    ApplicationCache(settings.db_path).clear(key)
    return credential_store(settings).delete(key)


@mcp.tool()
@user_errors
async def greenhouse_mark_inactive(application_id: str, account: str | None = None) -> Application:
    """Move an application to the inactive list in MyGreenhouse, e.g. after the employer rejected it.

    This only changes the user's own MyGreenhouse tracker: the employer is not notified and nothing is
    withdrawn. There is no undo in the site's UI, so only do it when the application is really over
    (a rejection email, or the user says so). The id comes from greenhouse_get_applications.

    Args:
        application_id: the application's id (not the job id).
        account: the account key. Omit if there is only one.
    """
    settings = GreenhouseSettings()
    key = select_accounts(settings, account)[0]
    cache = ApplicationCache(settings.db_path)
    known = {a.id for a in cache.load([key])}
    if not application_id.isdigit() or application_id not in known:
        raise ValueError(
            f"{application_id!r} isn't one of the account's applications; list them with "
            "greenhouse_get_applications first."
        )
    async with _client(settings, key) as client:
        await client.mark_inactive(application_id)
        apps = await client.applications()
    cache.replace(key, apps)
    return next(a for a in apps if a.id == application_id)


class AccountSync(BaseModel):
    account: str
    synced_at: str | None
    from_cache: bool
    error: str | None = None


class ApplicationsResult(BaseModel):
    applications: list[Application]
    accounts: list[AccountSync]


class DeltaResult(BaseModel):
    deltas: dict[str, Delta]  # account -> changes since that account's last committed delta
    errors: dict[str, str]


async def _sync_one(
    settings: GreenhouseSettings, cache: ApplicationCache, key: str, *, force: bool
) -> AccountSync:
    ttl = timedelta(minutes=settings.cache_ttl_minutes)
    last = cache.synced_at(key)
    if not force and is_fresh(last, ttl):
        return AccountSync(account=key, synced_at=last.isoformat() if last else None, from_cache=True)
    try:
        meta = credential_store(settings).list()[key]
        template = meta.get("mail_template") or MAIL_TEMPLATE
        async with GreenhouseClient(key, meta["username"], template, settings) as client:
            apps = await client.applications()
        cache.replace(key, apps)
    except Exception as e:  # one broken account shouldn't hide the others; serve what we cached
        return AccountSync(
            account=key, synced_at=last.isoformat() if last else None, from_cache=True, error=str(e)
        )
    now = cache.synced_at(key)
    return AccountSync(account=key, synced_at=now.isoformat() if now else None, from_cache=False)


async def sync_accounts(settings: GreenhouseSettings, keys: list[str], *, force: bool) -> list[AccountSync]:
    cache = ApplicationCache(settings.db_path)
    return list(await asyncio.gather(*(_sync_one(settings, cache, k, force=force) for k in keys)))


@mcp.tool()
@user_errors
async def greenhouse_get_applications(
    company: str | None = None,
    account: str | None = None,
    state: Literal["active", "inactive", "all"] = "all",
    title: str | None = None,
    applied_after: date | None = None,
    refresh: bool = False,
) -> ApplicationsResult:
    """List the user's job applications made through Greenhouse, newest first.

    Status depends on the employer. Most don't share updates with MyGreenhouse, so the status is only
    "Active" or "Inactive" (closed or rejected). Employers that do share (the application's
    `sharingStatus`) report their latest stage or activity, e.g. "Applied"; so "Active" means no
    updates are available, while "Applied" means the employer shares and nothing has happened yet.
    Served from a local cache when recent (30 minutes by default);
    otherwise it logs in (Greenhouse emails a code, read from the AgentMail inbox; up to two minutes)
    and refreshes. Check `accounts[].from_cache` / `synced_at` for freshness and `accounts[].error`.

    Args:
        company: employer name or part of it, e.g. "stripe".
        account: the account key (see greenhouse_list_accounts). Omit for all.
        state: active = still open, inactive = closed or marked inactive.
        title: case-insensitive substring of the job title.
        applied_after: only applications submitted on or after this date (YYYY-MM-DD).
        refresh: ignore the cache and log in again now.
    """
    settings = GreenhouseSettings()
    keys = select_accounts(settings, account)
    statuses = await sync_accounts(settings, keys, force=refresh)
    apps = ApplicationCache(settings.db_path).load(keys)
    apps = filter_applications(apps, company=company, state=state, title=title, applied_after=applied_after)
    return ApplicationsResult(applications=apps, accounts=statuses)


@mcp.tool()
@user_errors
async def greenhouse_get_delta(account: str | None = None, commit: bool = True) -> DeltaResult:
    """Show what changed in your Greenhouse applications since the last time this ran.

    Always refreshes first. Reports new applications, ones that turned inactive, and removed ones. An
    account's first run has no previous snapshot, so everything is reported as new with baseline=true.

    Args:
        account: the account key. Omit for all.
        commit: save the current state as the new baseline. Pass false to peek without
            consuming the changes.
    """
    settings = GreenhouseSettings()
    keys = select_accounts(settings, account)
    statuses = await sync_accounts(settings, keys, force=True)
    cache = ApplicationCache(settings.db_path)
    deltas: dict[str, Delta] = {}
    errors: dict[str, str] = {}
    for st in statuses:
        if st.error:
            errors[st.account] = st.error
            continue  # don't diff stale data against the baseline
        apps = cache.load([st.account])
        store = SnapshotStore(settings.snapshot_path(st.account))
        taken_at, old = store.load()
        deltas[st.account] = diff(taken_at, old, apps)
        if commit:
            store.save(apps)
    return DeltaResult(deltas=deltas, errors=errors)


@mcp.tool()
@user_errors
async def greenhouse_get_status_history(
    account: str | None = None, application_id: str | None = None, limit: int = 100
) -> list[StatusEvent]:
    """Status changes we've observed (Active -> Inactive), newest first. Local cache only, no login.

    `seen_at` is when this tool's cache first noticed the status, not when the employer changed it.

    Args:
        account: the account key. Omit for all.
        application_id: limit to one application (from greenhouse_get_applications).
        limit: maximum events to return.
    """
    settings = GreenhouseSettings()
    keys = select_accounts(settings, account)
    return ApplicationCache(settings.db_path).history(keys, application_id, limit)


def _client(settings: GreenhouseSettings, account: str | None) -> GreenhouseClient:
    key = select_accounts(settings, account)[0]
    meta = credential_store(settings).list()[key]
    return GreenhouseClient(key, meta["username"], meta.get("mail_template") or MAIL_TEMPLATE, settings)


class JobSearchResult(BaseModel):
    jobs: list[GreenhouseJob]
    page: int
    more_results: bool


@mcp.tool()
@user_errors
async def greenhouse_get_recommendations(account: str | None = None, limit: int = 20) -> list[GreenhouseJob]:
    """Jobs MyGreenhouse recommends for the user, based on their profile and activity.

    Live request each time (no cache). It logs in if the saved session expired (Greenhouse emails a
    code, read from the AgentMail inbox; up to two minutes).

    Args:
        account: the account key (see greenhouse_list_accounts). Omit if there is only one.
        limit: maximum jobs to return.
    """
    async with _client(GreenhouseSettings(), account) as client:
        return (await client.recommendations())[:limit]


@mcp.tool()
@user_errors
async def greenhouse_search_jobs(
    query: str,
    location: str | None = None,
    date_posted: str | None = None,
    page: int = 1,
    account: str | None = None,
) -> JobSearchResult:
    """Search jobs across every employer on Greenhouse, as the MyGreenhouse search page does.

    Results come 12 or so per page; ask for the next `page` while `more_results` is true. Live request,
    so it needs a login (Greenhouse emails a code, read from the AgentMail inbox; up to two minutes,
    but a saved session is normally reused). Each job has `applied_on` if the user already applied.

    Args:
        query: job title or keywords, e.g. "software engineering intern".
        location: a city, state or country, e.g. "San Francisco" or "California" or "India". It is
            looked up the way the site's location box does. Omit to search everywhere.
        date_posted: recency filter. Known value: "past_five_days".
        page: 1-based page number.
        account: the account key. Omit if there is only one.
    """
    async with _client(GreenhouseSettings(), account) as client:
        params: dict[str, Any] = {"query": query}
        if location:
            params |= await client.resolve_location(location)
        if date_posted:
            params["date_posted"] = date_posted
        jobs, more = await client.search_jobs(params, page)
    return JobSearchResult(jobs=jobs, page=page, more_results=more)


def main() -> None:
    mcp.run()
