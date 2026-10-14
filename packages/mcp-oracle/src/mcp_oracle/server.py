import asyncio
import functools
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Literal

from ats_core import (
    Application,
    ApplicationCache,
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

from mcp_oracle.accounts import OracleAccount, discover_api_host
from mcp_oracle.auth import AuthError
from mcp_oracle.client import OracleClient
from mcp_oracle.config import OracleSettings

mcp = MCPServer("oracle")


def user_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """mcp 2.x hides ordinary exceptions as crashes; only ToolError messages reach the model.
    Expected failures (bad input, unknown company, no mailbox, wrong code) must say what went wrong."""

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


def credential_store(settings: OracleSettings) -> CredentialStore:
    return CredentialStore("oracle", settings.config_dir)


def select_accounts(
    settings: OracleSettings, account: str | None = None, company: str | None = None
) -> list[str]:
    """Which saved accounts (hosts) a request is about: one by host, those matching a company, or all."""
    saved = credential_store(settings).list()
    if not saved:
        raise ValueError("No Oracle accounts saved. Use oracle_add_account first.")
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


def load_account(settings: OracleSettings, key: str) -> tuple[OracleAccount, str, str]:
    """(account, login email, mail template id)."""
    meta = credential_store(settings).list()[key]
    acct = OracleAccount.from_key(key, meta)
    return acct, meta["username"], meta.get("mail_template") or acct.default_mail_template


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
async def oracle_add_account(
    url: str, email: str, company: str | None = None, mail_template: str | None = None
) -> dict[str, str]:
    """Add an Oracle Recruiting Cloud employer. Nothing secret is stored.

    These sites log in with an email address and a one-time code. The code is read from the user's
    AgentMail inbox (set up with `work-mcp mailbox`), so the user must forward the employer's
    emails there. The first fetch triggers a code email and can take up to two minutes.

    Args:
        url: the careers site's sign-in or any page URL containing /sites/<SITE>/, e.g.
            https://careers.example.com/en/sites/CX_1/my-profile/sign-in
        email: the candidate account's email address.
        company: display name, e.g. "Acme". Defaults to a guess from the URL.
        mail_template: which email template to use to find the code (defaults to the company name;
            see ats_core.mailbox). Only needed if the employer's emails need special matching.
    """
    acct = await asyncio.to_thread(discover_api_host, url)
    meta = acct.meta("typed", company, mail_template)
    credential_store(OracleSettings()).save_account(acct.key, email, meta)
    return {"account": acct.key, "company": meta["company"], "email": email, "site": acct.site}


@mcp.tool()
@user_errors
async def oracle_list_accounts() -> dict[str, dict[str, str]]:
    """List the Oracle employers the user has registered: the index of known companies.

    Maps each account (host) to its company name, site, email, when it was added and how.
    Use the host as the `account` argument of other tools.
    """
    return credential_store(OracleSettings()).list()


@mcp.tool()
@user_errors
async def oracle_set_company(account: str, company: str) -> bool:
    """Set the display name for a registered account (its host), e.g. "Acme"."""
    return credential_store(OracleSettings()).update_meta(account, company=company.strip())


@mcp.tool()
@user_errors
async def oracle_delete_account(account: str) -> bool:
    """Forget an account and its cached session and data (its host)."""
    settings = OracleSettings()
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
    settings: OracleSettings, cache: ApplicationCache, key: str, *, force: bool
) -> AccountSync:
    company = credential_store(settings).list()[key].get("company", "")
    ttl = timedelta(minutes=settings.cache_ttl_minutes)
    last = cache.synced_at(key)
    if not force and is_fresh(last, ttl):
        return AccountSync(
            account=key, company=company, synced_at=last.isoformat() if last else None, from_cache=True
        )
    try:
        acct, email, template = load_account(settings, key)
        async with OracleClient(acct, email, template, settings) as client:
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


async def sync_accounts(settings: OracleSettings, keys: list[str], *, force: bool) -> list[AccountSync]:
    cache = ApplicationCache(settings.db_path)
    return list(await asyncio.gather(*(_sync_one(settings, cache, k, force=force) for k in keys)))


def with_company(settings: OracleSettings, apps: list[Application]) -> list[Application]:
    index = credential_store(settings).list()
    return [a.model_copy(update={"company": index.get(a.account, {}).get("company", "")}) for a in apps]


@mcp.tool()
@user_errors
async def oracle_get_applications(
    company: str | None = None,
    account: str | None = None,
    state: Literal["active", "inactive", "all"] = "all",
    status: str | None = None,
    title: str | None = None,
    applied_after: date | None = None,
    refresh: bool = False,
) -> ApplicationsResult:
    """List job applications across all registered Oracle employers (or just one), newest first.

    Served from a local cache when it is recent (30 minutes by default); otherwise it logs in
    (the employer emails a code, read from the AgentMail inbox; up to two minutes) and refreshes.
    Check `accounts[].from_cache` / `synced_at` for freshness and `accounts[].error` for employers
    that couldn't be refreshed (their cached data is still returned). Drafts have status "Draft".

    Args:
        company: employer name or part of it, e.g. "acme". Matches the registered
            company name or host (see oracle_list_accounts). Omit to include every employer.
        account: exact host, e.g. careers.example.com. Overrides `company`.
        state: active = still in progress, inactive = closed (e.g. Not Progressing, Withdrawn).
        status: case-insensitive substring of the status text, e.g. "under review".
        title: case-insensitive substring of the job title.
        applied_after: only applications submitted on or after this date (YYYY-MM-DD).
        refresh: ignore the cache and log in again now.
    """
    settings = OracleSettings()
    keys = select_accounts(settings, account, company)
    statuses = await sync_accounts(settings, keys, force=refresh)
    apps = with_company(settings, ApplicationCache(settings.db_path).load(keys))
    apps = filter_applications(apps, state=state, status=status, title=title, applied_after=applied_after)
    return ApplicationsResult(applications=apps, accounts=statuses)


@mcp.tool()
@user_errors
async def oracle_get_delta(
    company: str | None = None, account: str | None = None, commit: bool = True
) -> DeltaResult:
    """Show what changed in your applications since the last time this ran, per employer.

    Always refreshes from the careers site first. Reports new applications, status changes, and
    removed ones. An employer's first run has no previous snapshot, so everything is reported as
    new with baseline=true.

    Args:
        company: employer name or part of it. Omit to check every registered employer.
        account: exact host. Overrides `company`.
        commit: save the current state as the new baseline. Pass false to peek without
            consuming the changes.
    """
    settings = OracleSettings()
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
async def oracle_get_status_history(
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
        application_id: limit to one application (from oracle_get_applications).
        limit: maximum events to return.
    """
    settings = OracleSettings()
    keys = select_accounts(settings, account, company)
    return ApplicationCache(settings.db_path).history(keys, application_id, limit)


def main() -> None:
    mcp.run()
