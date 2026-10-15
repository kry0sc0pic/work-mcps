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
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_applications(html: str) -> list[ListedApplication]:
    """Rows of the "Jobs I've applied to" table on the profile page (title, location, status, date)."""
    start = html.find("Jobs I")
    if start < 0:
        return []
    table = re.search(r"<tbody>(.*?)</tbody>", html[start:], re.S)
    if not table:
        return []
    out: list[ListedApplication] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        link = re.search(r"jobId=(\d+)", cells[0]) if cells else None
        if len(cells) < 4 or not link:
            continue
        out.append(
            ListedApplication(
                job_id=link.group(1),
                title=_clean(cells[0]),
                location=_clean(cells[1]),
                status=_clean(cells[2]),
                applied_on=parse_date(_clean(cells[3])),
            )
        )
    return out
