"""A mailbox the servers can read one-time codes from, for sites that log in by emailing a code.

The user forwards the site's emails to an AgentMail inbox; we poll it through the AgentMail Python SDK
(never the AgentMail MCP: this runs inside an MCP server). The API key lives in the keychain, the inbox
id in a non-secret JSON file. `AGENTMAIL_API_KEY` / `AGENTMAIL_INBOX_ID` override both.
"""

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING

import keyring
from keyring.errors import PasswordDeleteError
from pydantic import BaseModel, ValidationError

from ats_core.credentials import SERVICE, CredentialStoreError, _keychain, default_config_dir

if TYPE_CHECKING:
    from agentmail import AgentMail

_KEY = "agentmail:api-key"
DEFAULT_CODE = r"(?<!\d)(\d{6})(?!\d)"


class EmailTemplate(BaseModel):
    """How to recognise one company's login-code email and pull the code out of it."""

    contains: str | None = None  # case-insensitive text that must appear in sender, subject or body
    pattern: str = DEFAULT_CODE  # regex; group 1 (or the whole match) is the code
    timeout: float = 120


# Built-in templates, keyed by a lowercase company id. Add or override your own in
# `<config>/email-templates.json` (same shape: {"acme": {"contains": "acme", "pattern": "..."}}).
# Fill these in from a real email; until then a company gets the generic 6-digit template.
BUILTIN_TEMPLATES: dict[str, EmailTemplate] = {
    "greenhouse": EmailTemplate(contains="greenhouse", pattern=r"(?<!\d)(\d{8})(?!\d)"),  # 8-digit code
}


_used: set[str] = set()  # messages whose code was returned; a later login must wait for a new email


class MailboxError(RuntimeError):
    """The mailbox isn't configured, or no matching email arrived. The message says what to do."""


def _config_path():
    return default_config_dir() / "mailbox.json"


def load_template(company: str) -> EmailTemplate:
    """User file first, then built-ins, else a generic template that matches on the company name."""
    key = company.strip().lower()
    try:
        raw = json.loads((default_config_dir() / "email-templates.json").read_text(encoding="utf-8"))
        if key in raw:
            return EmailTemplate.model_validate(raw[key])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    except ValidationError as e:
        raise MailboxError(f"email-templates.json entry for {key!r} is invalid: {e}") from e
    return BUILTIN_TEMPLATES.get(key) or EmailTemplate(contains=key)


def save_mailbox(api_key: str, inbox_id: str) -> None:
    with _keychain():
        keyring.set_password(SERVICE, _KEY, api_key)
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"inbox_id": inbox_id}, indent=2), encoding="utf-8")


def delete_mailbox() -> None:
    with _keychain():
        try:
            keyring.delete_password(SERVICE, _KEY)
        except PasswordDeleteError:
            pass
    _config_path().unlink(missing_ok=True)


def load_mailbox() -> tuple[str, str] | None:
    """(api_key, inbox_id), or None if either is missing."""
    inbox = os.environ.get("AGENTMAIL_INBOX_ID")
    if not inbox:
        try:
            inbox = json.loads(_config_path().read_text(encoding="utf-8")).get("inbox_id")
        except (FileNotFoundError, json.JSONDecodeError):
            inbox = None
    key = os.environ.get("AGENTMAIL_API_KEY")
    if not key:
        try:
            with _keychain():
                key = keyring.get_password(SERVICE, _KEY)
        except CredentialStoreError:
            key = None
    return (key, inbox) if key and inbox else None


def _client(api_key: str) -> "AgentMail":
    from agentmail import AgentMail

    return AgentMail(api_key=api_key)


def check_mailbox(api_key: str, inbox_id: str) -> None:
    """Raise MailboxError unless the key can see the inbox."""
    try:
        _client(api_key).inboxes.get(inbox_id=inbox_id)
    except Exception as e:  # SDK raises ApiError subclasses; the message is what matters here
        raise MailboxError(f"AgentMail rejected the key or inbox id: {e}") from e


def _text(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script).*?</\1>", " ", html)))


def find_code(text: str, pattern: str = DEFAULT_CODE) -> str | None:
    m = re.search(pattern, text)
    return (m.group(1) if m.groups() else m.group(0)) if m else None


def wait_for_code(company: str, *, since: datetime, interval: float = 4) -> str:
    """Poll the inbox for a message newer than `since` that matches `company`'s template; return its code.

    The template's `contains` must appear in the sender, subject or body, so one inbox can serve several
    sites. Forwarding may rewrite the sender, hence not matching on it alone. The email body is
    untrusted: only the matched code leaves this function.
    """
    template = load_template(company)
    cfg = load_mailbox()
    if not cfg:
        raise MailboxError(
            "No AgentMail inbox is configured. Run `work-mcp mailbox` to save the API key and inbox id "
            "(or set AGENTMAIL_API_KEY and AGENTMAIL_INBOX_ID)."
        )
    api_key, inbox_id = cfg
    client = _client(api_key)
    needle = template.contains.lower() if template.contains else None
    seen: set[str] = set()
    deadline = time.monotonic() + template.timeout
    after = since.astimezone(UTC) - timedelta(seconds=2)  # small allowance for clock skew
    while True:
        try:
            items = client.inboxes.messages.list(inbox_id=inbox_id, after=after, ascending=False, limit=10)
            for item in items.messages:
                if item.message_id in seen or item.message_id in _used:
                    continue
                seen.add(item.message_id)
                if (
                    item.timestamp.astimezone(UTC) < after
                ):  # enforce `since` ourselves, not just via the query
                    continue
                msg = client.inboxes.messages.get(inbox_id=inbox_id, message_id=item.message_id)
                body = msg.extracted_text or msg.text or _text(msg.extracted_html or msg.html or "")
                haystack = f"{msg.from_ or ''} {msg.subject or ''} {body}"
                if needle and needle not in haystack.lower():
                    continue
                if code := find_code(f"{msg.subject or ''}\n{body}", template.pattern):
                    _used.add(item.message_id)
                    return code
        except MailboxError:
            raise
        except Exception as e:
            raise MailboxError(f"Couldn't read the AgentMail inbox: {e}") from e
        if time.monotonic() >= deadline:
            raise MailboxError(
                f"No email with a code arrived within {int(template.timeout)}s. Check that forwarding "
                "is active and the message isn't in the inbox's spam."
            )
        time.sleep(interval)


def now() -> datetime:
    return datetime.now(UTC)
