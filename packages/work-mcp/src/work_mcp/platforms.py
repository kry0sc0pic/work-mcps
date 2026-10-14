"""Registry of ATS platforms the wizard can set up. Add a platform by implementing `Platform`."""

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from ats_core import CredentialStore
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

    def owns(self, url: str) -> bool:
        """Does this URL belong to the platform, even if it lacks details needed to log in?"""
        ...

    def parse(self, url: str, company: str | None = None, source: str = "typed") -> AccountRef:
        """Raises ValueError when the URL lacks something needed to log in."""
        ...

    def store(self) -> CredentialStore: ...


class Workday:
    id = "workday"
    label = "Workday"
    server_script = "mcp-workday"
    _host = re.compile(r"(^|\.)myworkdayjobs\.com$")

    def owns(self, url: str) -> bool:
        raw = url.strip()
        host = urlparse(raw if "://" in raw else "https://" + raw).hostname or ""
        return bool(self._host.search(host.lower()))

    def parse(self, url: str, company: str | None = None, source: str = "typed") -> AccountRef:
        acct = WorkdayAccount.from_url(url)
        meta = acct.meta(source, company)
        return AccountRef(key=acct.key, meta=meta, label=f"{meta['company']} ({acct.host})")

    def store(self) -> CredentialStore:
        return CredentialStore("workday", WorkdaySettings().config_dir)


PLATFORMS: dict[str, Platform] = {p.id: p for p in (Workday(),)}
