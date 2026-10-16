import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Literal

from ats_core import (
    Application,
    ApplicationCache,
    Credential,
    CredentialStore,
    Delta,
    SnapshotStore,
    StatusEvent,
    diff,
    is_fresh,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from mcp_avature.accounts import AvatureAccount
from mcp_avature.auth import AuthError
from mcp_avature.client import AvatureClient
from mcp_avature.config import AvatureSettings
from mcp_avature.waf import WafError

# httpx logs full request URLs at INFO, which would put list-feed signatures in the log.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = MCPServer("avature")


def user_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """mcp 2.x hides ordinary exceptions as crashes; only ToolError messages reach the model.
    Expected failures (bad input, unknown company, unknown company) must say what went wrong."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (ValueError, AuthError, WafError, RuntimeError) as e:
            raise ToolError(str(e)) from e

    return wrapper


def credential_store(settings: AvatureSettings) -> CredentialStore:
    return CredentialStore("avature", settings.config_dir)


def select_accounts(
    settings: AvatureSettings, account: str | None = None, company: str | None = None
) -> list[str]:
    """Which saved accounts (hosts) a request is about: one by host, those matching a company, or all."""
    saved = credential_store(settings).list()
    if not saved:
        raise ValueError(
            "No Avature accounts saved. Run `work-mcp add-avature <portal URL>` in a terminal first."
        )
    if account is not None:
        if account not in saved:
            raise ValueError(f"Unknown account {account!r}. Saved: {', '.join(sorted(saved))}")
        return [account]
    if company:
        needle = company.strip().lower()
        hits = [k for k, m in saved.items() if needle in m.get("company", "").lower() or needle in k.lower()]
        if not hits:
            known = ", ".join(sorted(m.get("company") or k for k, m in saved.items()))
            raise ValueError(f"No registered company matches {company!r}. Registered: {known}")
        return sorted(hits)
    return sorted(saved)


def load_account(settings: AvatureSettings, key: str) -> tuple[AvatureAccount, Credential]:
    store = credential_store(settings)
    cred = store.get(key)
    if cred is None:
        raise ValueError(f"Credentials for {key} are missing from the keychain; save them again.")
    portal = credential_store(settings).list().get(key, {}).get("portal")
    return AvatureAccount.from_key(key, portal), cred


def filter_applications(
    apps: list[Application],
    *,
    state: Literal["active", "inactive", "all"] = "all",
    status: str | None = None,
    title: str | None = None,
    applied_after: date | None = None,
) -> list[Application]:
    out = apps
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
@user_errors
async def avature_list_accounts() -> dict[str, dict[str, str]]:
    """List the Avature employers the user has registered: the index of known companies.

    Maps each account (host) to its company name, username, when it was added and how
    (cli, csv-import). Passwords are never returned. Use the host as the `account`
    argument of other tools.
    """
    return credential_store(AvatureSettings()).list()


@mcp.tool()
@user_errors
async def avature_set_company(account: str, company: str) -> bool:
    """Set the display name for a registered account, e.g. ("acme.avature.net", "Acme Air").

    The default is guessed from the URL and is wrong for some tenants, whose name differs from the company.
    """
    return credential_store(AvatureSettings()).update_meta(account, company=company.strip())


@mcp.tool()
@user_errors
async def avature_delete_credentials(account: str) -> bool:
    """Delete the saved credentials and cached session for an account (its host)."""
    settings = AvatureSettings()
    settings.session_path(account).unlink(missing_ok=True)
    settings.snapshot_path(account).unlink(missing_ok=True)
    ApplicationCache(settings.db_path).clear(account)
    return credential_store(settings).delete(account)


class AccountSync(BaseModel):
    account: str
    company: str
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
    settings: AvatureSettings, cache: ApplicationCache, key: str, *, force: bool
) -> AccountSync:
    meta = credential_store(settings).list()[key]
    company = meta.get("company", "")
    ttl = timedelta(minutes=settings.cache_ttl_minutes)
    last = cache.synced_at(key)
    if not force and is_fresh(last, ttl):
        return AccountSync(
            account=key, company=company, synced_at=last.isoformat() if last else None, from_cache=True
        )
    try:
        acct, cred = load_account(settings, key)
        async with AvatureClient(acct, cred, settings) as client:
            apps = await client.applications()
        cache.replace(key, apps)
    except Exception as e:  # one broken account shouldn't hide the others; serve what we cached
        return AccountSync(
            account=key,
            company=company,
            synced_at=last.isoformat() if last else None,
            from_cache=True,
            error=str(e),
        )
    now = cache.synced_at(key)
    return AccountSync(
        account=key, company=company, synced_at=now.isoformat() if now else None, from_cache=False
    )


async def sync_accounts(settings: AvatureSettings, keys: list[str], *, force: bool) -> list[AccountSync]:
    cache = ApplicationCache(settings.db_path)
    return list(await asyncio.gather(*(_sync_one(settings, cache, k, force=force) for k in keys)))


def with_company(settings: AvatureSettings, apps: list[Application]) -> list[Application]:
    index = credential_store(settings).list()
    return [a.model_copy(update={"company": index.get(a.account, {}).get("company", "")}) for a in apps]


@mcp.tool()
@user_errors
async def avature_get_applications(
    company: str | None = None,
    account: str | None = None,
    state: Literal["active", "inactive", "all"] = "all",
    status: str | None = None,
    title: str | None = None,
    applied_after: date | None = None,
    refresh: bool = False,
) -> ApplicationsResult:
    """List Avature job applications across all registered employers (or just one), newest first.

    The status is the portal's own label (e.g. "Qualifications Under Review") and `applied_on` the
    application date it shows.

    Served from a local cache when it is recent (30 minutes by default); otherwise it logs in with
    the saved credentials and refreshes. Check `accounts[].from_cache` / `synced_at` for freshness
    and `accounts[].error` for employers that couldn't be refreshed (their cached data is still
    returned).

    Args:
        company: employer name or part of it, e.g. "hooli". Matches the registered company name
            or host (see avature_list_accounts). Omit to include every registered employer.
        account: exact host, e.g. acme.avature.net. Overrides `company`.
        state: active = still in progress, inactive = closed (a keyword guess; trust the status text).
        applied_after: only applications submitted on or after this date (YYYY-MM-DD).
        status: case-insensitive substring of the status text, e.g. "under review".
        title: case-insensitive substring of the job title.
        refresh: ignore the cache and log in again now.
    """
    settings = AvatureSettings()
    keys = select_accounts(settings, account, company)
    statuses = await sync_accounts(settings, keys, force=refresh)
    apps = with_company(settings, ApplicationCache(settings.db_path).load(keys))
    apps = filter_applications(apps, state=state, status=status, title=title, applied_after=applied_after)
    return ApplicationsResult(applications=apps, accounts=statuses)


class AccountSchedule(BaseModel):
    account: str
    company: str
    events: list[dict[str, str]] = []
    interviews: list[dict[str, str]] = []
    error: str | None = None


async def _schedule_one(settings: AvatureSettings, key: str, company: str) -> AccountSchedule:
    try:
        acct, cred = load_account(settings, key)
        async with AvatureClient(acct, cred, settings) as client:
            events, interviews = await client.schedule()
    except Exception as e:  # one broken account shouldn't hide the others
        return AccountSchedule(account=key, company=company, error=str(e))
    return AccountSchedule(account=key, company=company, events=events, interviews=interviews)


@mcp.tool()
@user_errors
async def avature_get_schedule(
    company: str | None = None, account: str | None = None
) -> list[AccountSchedule]:
    """Upcoming interviews and registered events from each employer's portal profile page.

    Always fetched live (logs in with the saved credentials); it isn't cached. Each row is keyed by the
    portal's own column names (e.g. Position, Interview date, Interview time / Event, Location, Date, Status).
    Empty lists mean the portal says there is nothing scheduled. Poll this to notice a new interview.

    Args:
        company: employer name or part of it. Omit to check every registered employer.
        account: exact host. Overrides `company`.
    """
    settings = AvatureSettings()
    keys = select_accounts(settings, account, company)
    index = credential_store(settings).list()
    return list(
        await asyncio.gather(*(_schedule_one(settings, k, index.get(k, {}).get("company", "")) for k in keys))
    )


@mcp.tool()
@user_errors
async def avature_get_delta(
    company: str | None = None, account: str | None = None, commit: bool = True
) -> DeltaResult:
    """Show what changed in your applications since the last time this ran, per employer.

    Always refreshes from Avature first. Reports new applications, status changes, and removed
    ones. An employer's first run has no previous snapshot, so everything is reported as new
    with baseline=true.

    Args:
        company: employer name or part of it. Omit to check every registered employer.
        account: exact host. Overrides `company`.
        commit: save the current state as the new baseline. Pass false to peek without
            consuming the changes.
    """
    settings = AvatureSettings()
    keys = select_accounts(settings, account, company)
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
async def avature_get_status_history(
    company: str | None = None,
    account: str | None = None,
    application_id: str | None = None,
    limit: int = 100,
) -> list[StatusEvent]:
    """Status changes we've observed, newest first. Reads the local cache only (no login).

    `seen_at` is when this tool's cache first noticed the status, not when the employer changed it.

    Args:
        company: employer name or part of it. Omit for all employers.
        account: exact host. Overrides `company`.
        application_id: limit to one application (from avature_get_applications).
        limit: maximum events to return.
    """
    settings = AvatureSettings()
    keys = select_accounts(settings, account, company)
    return ApplicationCache(settings.db_path).history(keys, application_id, limit)


def main() -> None:
    mcp.run()
