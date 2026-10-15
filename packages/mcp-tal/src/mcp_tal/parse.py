"""HTML scraping for TAL.net's legacy candidate pages (server-rendered; there is no JSON API)."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser

_BLOCKS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr", "ul", "ol"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    """Readable text: block tags become line breaks and runs of whitespace collapse."""
    p = _Text()
    p.feed(html)
    p.close()
    text = "".join(p.parts).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def is_logged_in(html: str) -> bool:
    """Signed-in pages carry the user menu's logout link; the signed-out page only has a hidden form."""
    return 'class="logout_button"' in html


def needs_check(html: str) -> bool:
    """The site's 'Quick Check Needed' interstitial (an altcha proof-of-work page)."""
    return "altcha-widget" in html and "originalRequestToken" in html


class _Form(HTMLParser):
    def __init__(self, form_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form" and a.get("id") == self.form_id:
            self._inside = True
            self.action = a.get("action")
        elif tag == "input" and self._inside and a.get("type") == "hidden" and (name := a.get("name")):
            self.fields[name] = a.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._inside = False


def hidden_form(html: str, form_id: str) -> tuple[str | None, dict[str, str]]:
    """(action, hidden inputs) of the form with this id."""
    p = _Form(form_id)
    p.feed(html)
    return p.action, p.fields


def hidden_input(html: str, name: str) -> str | None:
    m = re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="([^"]*)"', html)
    return m.group(1) if m else None


@dataclass
class ListedApplication:
    id: str
    title: str
    status: str


def parse_application_list(html: str) -> list[ListedApplication]:
    """Rows of the "Applications" table: title, status label, and a link ending in /application/<id>."""
    out: list[ListedApplication] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        link = re.search(r"/application/(\d+)(?=[\"'/?#])", row)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if not link or len(cells) < 2:
            continue
        out.append(ListedApplication(link.group(1), html_text(cells[0]), html_text(cells[1])))
    return out


@dataclass
class ApplicationPage:
    title: str
    heading: str  # e.g. "Application Update"
    message: str  # the status description the employer shows


def parse_application(html: str) -> ApplicationPage:
    title = re.search(r'class="app-position"[^>]*>(.*?)</strong>', html, re.S)
    heading = re.search(r"<div class=['\"]app-status['\"]>(.*?)</div>", html, re.S)
    desc = re.search(r"<div class=['\"]app-status-desc['\"]>(.*?)</div>", html, re.S)
    head = html_text(heading.group(1)) if heading else ""
    # The h2 reads "<job title>: Application Update"; keep what follows the colon.
    return ApplicationPage(
        title=html_text(title.group(1)) if title else "",
        heading=head.rsplit(":", 1)[-1].strip(),
        message=html_text(desc.group(1)) if desc else "",
    )


@dataclass
class Message:
    id: str
    sent_on: date | None
    subject: str
    status: str


def parse_date(s: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%Y, %H:%M"):  # the portal is en-GB: day first
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_correspondence(html: str) -> list[Message]:
    out: list[Message] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        link = re.search(r"/correspondence/email/(\d+)", row)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if link and len(cells) >= 3:
            out.append(
                Message(
                    link.group(1), parse_date(html_text(cells[0])), html_text(cells[1]), html_text(cells[2])
                )
            )
    return out


@dataclass
class Email:
    sent_at: str
    subject: str
    body: str


def parse_email(html: str) -> Email:
    subject = re.search(r"Email Details\s*(.*?)</h2>", html, re.S)
    sent = re.search(r'<span class="corr">Date Sent</span>\s*<span>(.*?)</span>', html, re.S)
    body = re.search(r'<div class="rich_text">(.*?)</div>\s*<hr', html, re.S)
    return Email(
        sent_at=html_text(sent.group(1)) if sent else "",
        subject=html_text(subject.group(1)) if subject else "",
        body=html_text(body.group(1)) if body else "",
    )


def main_text(html: str) -> str:
    """Text of the page's main content area."""
    m = re.search(r"""<div[^>]*id=["']main-content["'][^>]*>""", html)
    if not m:
        return html_text(html)
    end = html.find('<div class="footer', m.end())
    return html_text(html[m.end() : end if end > 0 else None])
