"""HTML scraping for Avature's candidate portal (server-rendered)."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape


@dataclass
class ListedApplication:
    job_id: str
    title: str
    location: str
    status: str
    applied_on: date | None


def is_logged_in(html: str) -> bool:
    """The signed-in menu links to .../Logout; the login page doesn't."""
    return bool(re.search(r"href=\"[^\"]*/Logout/?\"", html))


def is_login_page(html: str) -> bool:
    return 'name="username"' in html and 'name="password"' in html


def parse_date(text: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _column(headers: list[str], word: str, default: int | None) -> int | None:
    # Portals with a real header row name their columns; others only have a lone title header.
    if len(headers) < 3:
        return default
    return next((i for i, h in enumerate(headers) if word in h), None)


def _cell(cells: list[str], i: int | None) -> str:
    return cells[i] if i is not None and i < len(cells) else ""


def parse_applications(html: str) -> list[ListedApplication]:
    """Rows of the profile page's applications table. Columns differ per portal (one has title, location,
    status, date; another has position, application date, status, withdraw), so they follow the header text."""
    out: list[ListedApplication] = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        body = re.search(r"<tbody>(.*?)</tbody>", table, re.S)
        if not body or "JobDetail" not in body.group(1):
            continue
        headers = [_clean(h).lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        loc_i = _column(headers, "location", 1)
        status_i = _column(headers, "status", 2)
        date_i = _column(headers, "date", 3)
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S):
            cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            raw = re.search(r"<td[^>]*>(.*?)</td>", row, re.S)
            link = re.search(r"JobDetail(?:\?jobId=|/[^\"'?]*/)(\d+)", raw.group(1)) if raw else None
            if not link or status_i is None or status_i >= len(cells):
                continue

            out.append(
                ListedApplication(
                    job_id=link.group(1),
                    title=cells[0],
                    location=_cell(cells, loc_i),
                    status=cells[status_i],
                    applied_on=parse_date(_cell(cells, date_i)),
                )
            )
    return out
