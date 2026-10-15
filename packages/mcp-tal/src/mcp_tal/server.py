import asyncio
import functools
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

from mcp_tal.accounts import TalAccount
from mcp_tal.auth import AuthError
from mcp_tal.client import TalApplicationDetail, TalClient
from mcp_tal.config import TalSettings
from mcp_tal.parse import Email

mcp = MCPServer("tal")


def user_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """mcp 2.x hides ordinary exceptions as crashes; only ToolError messages reach the model.
    Expected failures (bad input, unknown company, unknown company) must say what went wrong."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (ValueError, AuthError, RuntimeError) as e:
            raise ToolError(str(e)) from e

    return wrapper


def credential_store(settings: TalSettings) -> CredentialStore:
    return CredentialStore("tal", settings.config_dir)


def select_accounts(
    settings: TalSettings, account: str | None = None, company: str | None = None
) -> list[str]:
    """Which saved accounts (hosts) a request is about: one by host, those matching a company, or all."""
    saved = credential_store(settings).list()
    if not saved:
        raise ValueError(
            "No TAL.net accounts saved. Run `work-mcp add-tal <portal URL>` in a terminal first."
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


def load_account(settings: TalSettings, key: str) -> tuple[TalAccount, Credential]:
    store = credential_store(settings)
    cred = store.get(key)
    if cred is None:
        raise ValueError(f"Credentials for {key} are missing from the keychain; save them again.")
    return TalAccount.from_key(key), cred


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
async def tal_list_accounts() -> dict[str, dict[str, str]]:
    """List the TAL.net employers the user has registered: the index of known companies.

    Maps each account (host) to its company name, username, when it was added and how
    (cli, csv-import). Passwords are never returned. Use the host as the `account`
    argument of other tools.
    """
    return credential_store(TalSettings()).list()


@mcp.tool()
@user_errors
async def tal_set_company(account: str, company: str) -> bool:
    """Set the display name for a registered account, e.g. tal_set_company("umbrella.wd5…", "Umbrella").

    The default is guessed from the URL and is wrong for some tenants, whose name differs from the company.
    """
    return credential_store(TalSettings()).update_meta(account, company=company.strip())


@mcp.tool()
@user_errors
async def tal_delete_credentials(account: str) -> bool:
    """Delete the saved credentials and cached session for an account (its host)."""
    settings = TalSettings()
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


async def _sync_one(settings: TalSettings, cache: ApplicationCache, key: str, *, force: bool) -> AccountSync:
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
        async with TalClient(acct, cred, settings) as client:
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


async def sync_accounts(settings: TalSettings, keys: list[str], *, force: bool) -> list[AccountSync]:
    cache = ApplicationCache(settings.db_path)
    return list(await asyncio.gather(*(_sync_one(settings, cache, k, force=force) for k in keys)))


def with_company(settings: TalSettings, apps: list[Application]) -> list[Application]:
    index = credential_store(settings).list()
    return [a.model_copy(update={"company": index.get(a.account, {}).get("company", "")}) for a in apps]


@mcp.tool()
@user_errors
async def tal_get_applications(
    company: str | None = None,
    account: str | None = None,
    state: Literal["active", "inactive", "all"] = "all",
    status: str | None = None,
    title: str | None = None,
    refresh: bool = False,
) -> ApplicationsResult:
    """List TAL.net job applications across all registered employers (or just one), newest first.

    TAL.net shows no application date, so `applied_on` is empty. The status is the portal's label plus
    the employer's own message ("Application Update: ... currently under review").

    Served from a local cache when it is recent (30 minutes by default); otherwise it logs in with
    the saved credentials and refreshes. Check `accounts[].from_cache` / `synced_at` for freshness
    and `accounts[].error` for employers that couldn't be refreshed (their cached data is still
    returned).

    Args:
        company: employer name or part of it, e.g. "hooli". Matches the registered company name
            or host (see tal_list_accounts). Omit to include every registered employer.
        account: exact host, e.g. acme.tal.net. Overrides `company`.
        state: active = still in progress, inactive = closed (a keyword guess; trust the status text).
        status: case-insensitive substring of the status text, e.g. "under review".
        title: case-insensitive substring of the job title.
        refresh: ignore the cache and log in again now.
    """
    settings = TalSettings()
    keys = select_accounts(settings, account, company)
    statuses = await sync_accounts(settings, keys, force=refresh)
    apps = with_company(settings, ApplicationCache(settings.db_path).load(keys))
    apps = filter_applications(apps, state=state, status=status, title=title)
    return ApplicationsResult(applications=apps, accounts=statuses)


@mcp.tool()
@user_errors
async def tal_get_delta(
    company: str | None = None, account: str | None = None, commit: bool = True
) -> DeltaResult:
    """Show what changed in your applications since the last time this ran, per employer.

    Always refreshes from TAL.net first. Reports new applications, status changes, and removed
    ones. An employer's first run has no previous snapshot, so everything is reported as new
    with baseline=true.

    Args:
        company: employer name or part of it. Omit to check every registered employer.
        account: exact host. Overrides `company`.
        commit: save the current state as the new baseline. Pass false to peek without
            consuming the changes.
    """
    settings = TalSettings()
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
async def tal_get_status_history(
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
        application_id: limit to one application (from tal_get_applications).
        limit: maximum events to return.
    """
    settings = TalSettings()
    keys = select_accounts(settings, account, company)
    return ApplicationCache(settings.db_path).history(keys, application_id, limit)


@mcp.tool()
@user_errors
async def tal_get_application(application_id: str, account: str | None = None) -> TalApplicationDetail:
    """One application in full: the status message, the list of emails the employer sent, and the
    opportunity description. Logs in live (not cached). Only reads pages; nothing is marked as read.

    Args:
        application_id: from tal_get_applications.
        account: exact host if several portals are registered; otherwise it is found automatically.
    """
    settings = TalSettings()
    key = _owner(settings, application_id, account)
    acct, cred = load_account(settings, key)
    async with TalClient(acct, cred, settings) as client:
        return await client.application(application_id)


@mcp.tool()
@user_errors
async def tal_get_message(application_id: str, message_id: str, account: str | None = None) -> Email:
    """Read one email the employer sent about an application (ids from tal_get_application).

    Args:
        application_id: the application the email belongs to.
        message_id: the email's id from the `communications` list.
        account: exact host if several portals are registered; otherwise it is found automatically.
    """
    settings = TalSettings()
    key = _owner(settings, application_id, account)
    acct, cred = load_account(settings, key)
    async with TalClient(acct, cred, settings) as client:
        return await client.email(application_id, message_id)


def _owner(settings: TalSettings, application_id: str, account: str | None) -> str:
    """The account an application id belongs to: the named one, else the one whose cache holds it."""
    keys = select_accounts(settings, account)
    if account or len(keys) == 1:
        return keys[0]
    cache = ApplicationCache(settings.db_path)
    for key in keys:
        if any(a.id == application_id for a in cache.load([key])):
            return key
    raise ValueError(
        f"No cached application {application_id}; run tal_get_applications first or pass `account`."
    )


def main() -> None:
    mcp.run()
