"""HTML scraping for Yello's candidate portal (server-rendered)."""

import re
from dataclasses import dataclass
from html import unescape


@dataclass
class ListedApplication:
    id: str
    requisition_id: str
    title: str
    status: str


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def is_login_page(html: str) -> bool:
    return 'name="username"' in html and 'name="password"' in html


def is_logged_in(html: str) -> bool:
    """The signed-in home page has the "My Applications" heading; the sign-in page never does."""
    return "candidatePortal__heading" in html and not is_login_page(html)


def hidden_form(html: str) -> tuple[str, dict[str, str]]:
    """(action, hidden fields) of the CAS sign-in form (`execution` is a one-time webflow token)."""
    form = re.search(r"<form[^>]*>.*?</form>", html, re.S)
    if not form or not is_login_page(form.group(0)):
        return "", {}
    action = re.search(r'action="([^"]*)"', form.group(0))
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*type=\"hidden\"[^>]*>", form.group(0)):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = unescape(value.group(1)) if value else ""
    return (action.group(1) if action else ""), fields


def _section(html: str, heading: str) -> str:
    m = re.search(
        rf"<h2[^>]*candidatePortal__heading[^>]*>\s*{re.escape(heading)}\s*</h2>(.*?)"
        r'(?=<div class="candidatePortal__container|</main>)',
        html,
        re.S,
    )
    return m.group(1) if m else ""


def parse_applications(html: str) -> list[ListedApplication]:
    """Cards under "My Applications": the label is the job title, the text the stage. No dates are shown."""
    out: list[ListedApplication] = []
    for card in re.split(r'(?=<div class="candidatePortal__eventCard")', _section(html, "My Applications")):
        app_id = re.search(r'value="(\d+)"[^>]*data-id="application-id"', card)
        if not app_id:
            continue
        req = re.search(r'value="(\d+)"[^>]*data-id="requisition-id"', card)
        label = re.search(r'class="yello-label"[^>]*>(.*?)</div>', card, re.S)
        text = re.search(r'class="yello-text"[^>]*>(.*?)</div>', card, re.S)
        out.append(
            ListedApplication(
                id=app_id.group(1),
                requisition_id=req.group(1) if req else "",
                title=_clean(label.group(1)) if label else "",
                status=_clean(text.group(1)) if text else "",
            )
        )
    return out


SCHEDULE_SECTIONS = {"Invitations": "invitations", "My Schedule": "events", "My Interviews": "interviews"}


def parse_schedule(html: str) -> dict[str, list[str]]:
    """Text of each entry under Invitations / My Schedule / My Interviews. Only the empty state ("You
    currently have no ...") has been seen, so what a filled entry contains is unverified."""
    out: dict[str, list[str]] = {}
    for heading, key in SCHEDULE_SECTIONS.items():
        body = _section(html, heading)
        blocks = re.split(r'(?=<div class="candidatePortal__(?:eventCard|row)\b)', body)
        texts = [_clean(b) for b in blocks if "candidatePortal__" in b]
        out[key] = [t for t in texts if t and not t.lower().startswith("you currently have no")]
    return out
