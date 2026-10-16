"""HTML scraping for Avature's candidate portal (server-rendered)."""

import json
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
    closed: bool = False


def is_logged_in(html: str) -> bool:
    """The signed-in menu links to .../Logout; the login page doesn't."""
    return bool(re.search(r"href=\"[^\"]*/Logout/?\"", html))


def is_login_page(html: str) -> bool:
    return 'name="username"' in html and 'name="password"' in html


def parse_date(text: str) -> date | None:
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%d-%b-%Y"):
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
    status, date; another has position, application date, status, withdraw), so they follow the header
    text."""
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


def parse_schedule(html: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(events, interviews) from the profile page's "Event information" / "Interview information" tables.
    Rows are keyed by their header text. Only the empty state ("You have no upcoming events") has been seen,
    so a filled row's exact cells are unverified."""
    events: list[dict[str, str]] = []
    interviews: list[dict[str, str]] = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        headers = [_clean(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        low = [h.lower() for h in headers]
        if not headers or not ("event" in low or any("interview" in h for h in low)):
            continue
        target = events if "event" in low else interviews
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
            cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) >= 2 and "not-applied" not in row:
                target.append({h: c for h, c in zip(headers, cells, strict=False) if h and c})
    return events, interviews


@dataclass
class ListWidget:
    """A server-driven list on a portal page: `<list data-props='{...}'>` plus the portal id, which the
    page's JSON feed lives under (`/<portal id>/_portalList`)."""

    portal_id: str
    props: dict[str, object]


def parse_list_widget(html: str) -> ListWidget | None:
    tag = re.search(r"<list\s+data-props='(.*?)'\s*>", html, re.S)
    portal = re.search(r'name="avature\.portal\.id"\s+content="(\d+)"', html)
    if not tag or not portal:
        return None
    try:
        props = json.loads(unescape(tag.group(1)))
    except ValueError:
        return None
    return ListWidget(portal.group(1), props) if isinstance(props, dict) and props.get("uuid") else None


def list_query(widget: ListWidget, offset: int) -> dict[str, str]:
    """The feed's query string, as the page's script builds it from the widget props."""
    p = widget.props

    def flag(name: str) -> str:
        return "true" if p.get(name) else "false"

    query = {
        "uuid": str(p["uuid"]),
        "hasToIncludePaginationOptions": flag("hasToIncludePaginationOptions"),
        "allowListSorting": flag("allowListSorting"),
        "fetchJobIdInPeopleLists": flag("fetchJobIdInPeopleLists"),
        "listType": str(p.get("listType", "PersonList")),
        "offset": str(offset),
        "filters": json.dumps({"search": ""}, separators=(",", ":")),
        "sort": "",
        "sortDirection": "DESC",
        "recordsPerPage": str(p.get("recordsPerPage", 10)),
        "token": "",
        "allowFilteringFromUrlParams": flag("allowFilteringFromUrlParams"),
        "layout": str(p.get("layout", "custom")),
        "dynamicValueConfigs": json.dumps(p.get("dynamicValueConfigs") or [], separators=(",", ":")),
        "shouldAddBase64FileFields": flag("shouldAddBase64FileFields"),
        "searchMode": str(p.get("searchMode", "ResultsAndCount")),
        "qtvc": str(p.get("qtvc", "")),
        "pageUrlParams": "{}",
    }
    if p.get("links"):
        query["links"] = json.dumps(p["links"], separators=(",", ":"))
    return query


def parse_list_rows(data: dict[str, object], *, closed: bool) -> list[ListedApplication]:
    """Applications from a list feed reply. Fields are keyed by generated codes, so they are found by their
    column label (title, status, dates); the id is the linked folder, which is unique per application."""
    columns = data.get("columns")
    labels = (
        {str(c.get("code")): str(c.get("label", "")).lower() for c in columns if isinstance(c, dict)}
        if isinstance(columns, list)
        else {}
    )

    def code(*words: str) -> str | None:
        return next((c for w in words for c, lab in labels.items() if w in lab), None)

    title_c = code("external title", "linked folder")
    status_c = code("status")
    date_c = code("submitted date", "created date", "folder date")
    out: list[ListedApplication] = []
    results = data.get("results")
    for row in results if isinstance(results, list) else []:
        fields = row.get("fields", {})
        dyn = row.get("dynamicValues") or {}
        folder = dyn.get("linkedFolderId")

        def text(c: str | None, fields: dict[str, dict[str, str]] = fields) -> str:
            return str(fields.get(c, {}).get("stringValue", "")).strip() if c else ""

        title = text(title_c) or text("CORE_name_3")
        if folder is None or not title:
            continue
        out.append(
            ListedApplication(
                job_id=str(folder),
                title=title,
                location="",
                status=text(status_c),
                applied_on=parse_date(text(date_c)),
                closed=closed,
            )
        )
    return out
