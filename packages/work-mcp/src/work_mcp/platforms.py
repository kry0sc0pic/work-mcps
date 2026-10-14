"""Registry of ATS platforms the wizard can set up. Add a platform by implementing `Platform`."""

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx
from ats_core import CredentialStore
from mcp_oracle.accounts import OracleAccount, discover_api_host
from mcp_oracle.config import OracleSettings
from mcp_workday.accounts import WorkdayAccount
from mcp_workday.config import WorkdaySettings


@dataclass(frozen=True)
class AccountRef:
    key: str  # credential-store key, e.g. the Workday host
    meta: dict[str, str]  # non-secret extras saved in the index, e.g. {"site": ...}
    label: str  # shown to the user


class Platform(Protocol):
    id: str
    label: str
    server_script: str  # console script that starts this platform's MCP server
    uses_password: bool  # False for email-code logins: nothing to import from a password manager

    def owns(self, url: str) -> bool:
        """Does this URL belong to the platform, even if it lacks details needed to log in?"""
        ...

    def parse(self, url: str, company: str | None = None, source: str = "typed") -> AccountRef:
        """Raises ValueError when the URL lacks something needed to log in."""
        ...

    def key_for(self, url: str) -> str:
        """The credential-store key a URL maps to, even when `parse` can't resolve everything."""
        ...

    def discover(self, url: str, company: str | None = None, source: str = "typed") -> AccountRef:
        """Like `parse`, but may make public (unauthenticated) requests to find missing details.
        Raises ValueError if it can't."""
        ...

    def store(self) -> CredentialStore: ...


def sites_from_robots(robots_txt: str, host: str) -> list[str]:
    """Site names from `Sitemap: https://<host>/<Site>/siteMap.xml` lines, in file order."""
    pattern = re.compile(rf"^sitemap:\s*https?://{re.escape(host)}/([^/\s]+)/siteMap\.xml", re.I | re.M)
    seen: list[str] = []
    for site in pattern.findall(robots_txt):
        if site not in seen:
            seen.append(site)
    return seen


class Workday:
    id = "workday"
    label = "Workday"
    server_script = "mcp-workday"
    uses_password = True
    _host = re.compile(r"(^|\.)myworkdayjobs\.com$")

    def owns(self, url: str) -> bool:
        raw = url.strip()
        host = urlparse(raw if "://" in raw else "https://" + raw).hostname or ""
        return bool(self._host.search(host.lower()))

    def parse(self, url: str, company: str | None = None, source: str = "typed") -> AccountRef:
        acct = WorkdayAccount.from_url(url)
        meta = acct.meta(source, company)
        return AccountRef(key=acct.key, meta=meta, label=f"{meta['company']} ({acct.host})")

    def key_for(self, url: str) -> str:
        raw = url.strip()
        return (urlparse(raw if "://" in raw else "https://" + raw).hostname or "").lower()

    def discover(self, url: str, company: str | None = None, source: str = "typed") -> AccountRef:
        """Host-only URLs (common in password managers) don't name the site; robots.txt does."""
        try:
            return self.parse(url, company, source)
        except ValueError:
            pass
        host = self.key_for(url)
        try:
            resp = httpx.get(f"https://{host}/robots.txt", headers={"user-agent": "Mozilla/5.0"}, timeout=15)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ValueError(
                f"couldn't reach {host} to look up its site name ({e.__class__.__name__})"
            ) from e
        sites = sites_from_robots(resp.text, host)
        if not sites:
            raise ValueError(f"{host} doesn't list a careers site; add it with a full careers URL")
        return self.parse(f"https://{host}/{sites[0]}", company, source)

    def store(self) -> CredentialStore:
        return CredentialStore("workday", WorkdaySettings().config_dir)


class Oracle:
    """Oracle Recruiting Cloud sites: email + emailed code, no password."""

    id = "oracle"
    label = "Oracle Recruiting Cloud"
    server_script = "mcp-oracle"
    uses_password = False

    def owns(self, url: str) -> bool:
        raw = url.strip()
        u = urlparse(raw if "://" in raw else "https://" + raw)
        return bool(re.search(r"/sites/[A-Za-z0-9_-]+", u.path))

    def parse(self, url: str, company: str | None = None, source: str = "typed") -> AccountRef:
        # Custom domains need one public page fetch to find the Oracle server, so parse == discover.
        acct = discover_api_host(url)
        meta = acct.meta(source, company)
        return AccountRef(key=acct.key, meta=meta, label=f"{meta['company']} ({acct.host})")

    def key_for(self, url: str) -> str:
        return OracleAccount.host_and_site(url)[0]

    def discover(self, url: str, company: str | None = None, source: str = "typed") -> AccountRef:
        return self.parse(url, company, source)

    def store(self) -> CredentialStore:
        return CredentialStore("oracle", OracleSettings().config_dir)


PLATFORMS: dict[str, Platform] = {p.id: p for p in (Workday(), Oracle())}
