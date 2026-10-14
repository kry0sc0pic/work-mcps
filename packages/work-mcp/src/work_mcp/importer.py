"""Import ATS logins from a password-manager CSV export (Apple Passwords, Chrome, 1Password, Bitwarden)."""

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ats_core import Credential

from work_mcp.platforms import PLATFORMS, AccountRef, Platform

# Header names used by the common exporters, lowercased.
_ALIASES = {
    "url": ("url", "login_uri", "website", "web site", "login url", "origin"),
    "username": ("username", "login_username", "user name", "login", "email"),
    "password": ("password", "login_password"),
    "name": ("name", "title"),
}

Status = Literal["new", "update", "lookup", "unchanged", "conflict", "skipped"]


@dataclass
class PlannedItem:
    platform: Platform
    status: Status
    detail: str
    ref: AccountRef | None = None
    credential: Credential | None = None
    source_url: str = ""
    company_hint: str | None = None

    @property
    def writable(self) -> bool:
        return self.status in ("new", "update", "lookup")


@dataclass
class ImportPlan:
    items: list[PlannedItem]
    rows_total: int
    rows_ignored: int  # rows that matched no platform

    def writable(self) -> list[PlannedItem]:
        return [i for i in self.items if i.writable]


class CsvFormatError(ValueError):
    pass


def _column(headers: Sequence[str], field: str) -> str | None:
    lowered = {h.strip().lower(): h for h in headers}
    return next((lowered[a] for a in _ALIASES[field] if a in lowered), None)


def read_rows(path: Path) -> list[dict[str, str]]:
    """Rows normalised to {url, username, password}. Raises CsvFormatError on unknown layouts."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {field: _column(reader.fieldnames or [], field) for field in _ALIASES}
        missing = [f for f, c in cols.items() if c is None and f != "name"]
        if missing:
            raise CsvFormatError(
                f"Couldn't find a {'/'.join(missing)} column. Found: {', '.join(reader.fieldnames or [])}"
            )
        return [{field: (row.get(col or "") or "").strip() for field, col in cols.items()} for row in reader]


def _company_hint(name: str) -> str | None:
    """Password-manager entry titles are often the company name; ignore ones that are just a URL."""
    return None if not name or any(ch in name for ch in "./:@") else name


def build_plan(path: Path, platform_ids: list[str] | None = None) -> ImportPlan:
    platforms = [PLATFORMS[i] for i in (platform_ids or PLATFORMS)]
    rows = read_rows(path)
    items: list[PlannedItem] = []
    seen: dict[tuple[str, str], PlannedItem] = {}
    ignored = 0
    for row in rows:
        platform = next((p for p in platforms if row["url"] and p.owns(row["url"])), None)
        if platform is None:
            ignored += 1
            continue
        if not row["password"] or not row["username"]:
            items.append(
                PlannedItem(platform, "skipped", "no username or password saved", source_url=row["url"])
            )
            continue
        company = _company_hint(row["name"])
        cred = Credential(username=row["username"], password=row["password"])
        key = platform.key_for(row["url"])
        try:
            ref = platform.parse(row["url"], company, source="csv-import")
            item = PlannedItem(platform, "new", "", ref, cred, row["url"])
        except ValueError:
            # Owned by the platform but missing a detail (e.g. Workday host with no site name).
            # Still counts as one of its passwords; the detail is looked up once the user confirms.
            item = PlannedItem(platform, "lookup", "site name will be looked up", None, cred, row["url"])
            item.company_hint = company
        prior = seen.get((platform.id, key))
        if prior is None:
            seen[(platform.id, key)] = item
            items.append(item)
        elif prior.credential == cred:
            continue  # same login listed twice
        else:
            item.status, item.detail = "conflict", f"another login for {key} was already in the file"
            items.append(item)
    for item in items:
        if item.status == "new":
            _classify_against_store(item)
    return ImportPlan(items=items, rows_total=len(rows), rows_ignored=ignored)


def _classify_against_store(item: PlannedItem) -> None:
    assert item.ref and item.credential
    store = item.platform.store()
    if item.ref.key not in store.list():
        return
    existing = store.get(item.ref.key)
    saved_meta = store.list()[item.ref.key]
    same_site = saved_meta.get("site") == item.ref.meta.get("site")
    if existing == item.credential and same_site:
        item.status, item.detail = "unchanged", "already saved"
    else:
        item.status, item.detail = "update", "replaces the saved login"


def apply_plan(plan: ImportPlan, only_platforms: set[str] | None = None) -> int:
    """Save the writable items, optionally limited to some platform ids. Returns how many were saved."""
    n = 0
    for item in plan.writable():
        if only_platforms is not None and item.platform.id not in only_platforms:
            continue
        if item.status == "lookup":
            try:
                item.ref = item.platform.discover(item.source_url, item.company_hint, source="csv-import")
            except ValueError as e:
                item.status, item.detail = "skipped", str(e)
                continue
            item.status = "new"
        assert item.ref and item.credential
        item.platform.store().save(item.ref.key, item.credential, meta=item.ref.meta)
        n += 1
    return n
