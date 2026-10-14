import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ats_core import mailbox
from ats_core.mailbox import MailboxError


def fake_client(messages):
    for m in messages:
        m.setdefault("timestamp", datetime.now(UTC))
    inbox = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **kw: SimpleNamespace(
                messages=[SimpleNamespace(message_id=m["id"], timestamp=m["timestamp"]) for m in messages]
            ),
            get=lambda inbox_id, message_id: SimpleNamespace(
                **next(m for m in messages if m["id"] == message_id)
            ),
        )
    )
    return SimpleNamespace(inboxes=inbox)


def msg(id, subject, text, sender="x@y.com"):
    return {
        "id": id, "subject": subject, "extracted_text": text, "text": None,
        "extracted_html": None, "html": None, "from_": sender, "message_id": id,
    }  # fmt: skip


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("AGENTMAIL_API_KEY", "k")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "i@agentmail.to")


def test_save_and_load_roundtrip():
    assert mailbox.load_mailbox() is None
    mailbox.save_mailbox("key", "i@agentmail.to")
    assert mailbox.load_mailbox() == ("key", "i@agentmail.to")
    mailbox.delete_mailbox()
    assert mailbox.load_mailbox() is None


def test_template_lookup_prefers_user_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.platform", "linux")
    assert mailbox.load_template("Acme").contains == "acme"  # generic fallback
    d = tmp_path / "work-mcps"
    d.mkdir()
    (d / "email-templates.json").write_text(
        json.dumps({"acme": {"contains": "acme.com", "pattern": r"code: (\w{8})"}}), encoding="utf-8"
    )
    t = mailbox.load_template("acme")
    assert (t.contains, t.pattern) == ("acme.com", r"code: (\w{8})")


def test_wait_returns_code_from_matching_email_only(configured, monkeypatch):
    msgs = [msg("1", "Your newsletter", "Order 123456 shipped"), msg("2", "Sign in", "Acme code 654321")]
    monkeypatch.setattr(mailbox, "_client", lambda key: fake_client(msgs))
    assert mailbox.wait_for_code("acme", since=mailbox.now(), interval=0) == "654321"


def test_wait_times_out_with_advice(configured, monkeypatch, tmp_path):
    monkeypatch.setattr(mailbox, "_client", lambda key: fake_client([]))
    monkeypatch.setattr(mailbox, "load_template", lambda c: mailbox.EmailTemplate(contains="acme", timeout=0))
    with pytest.raises(MailboxError, match="forwarding"):
        mailbox.wait_for_code("acme", since=mailbox.now(), interval=0)


def test_wait_without_config_explains_setup():
    with pytest.raises(MailboxError, match="work-mcp mailbox"):
        mailbox.wait_for_code("acme", since=mailbox.now())


SAMPLE_EMAIL = """Hello Jane,

Thank you for your interest in working at Acme.

Before you get started with your application, please confirm your identity
using the one-time passcode below:

123456
This code will expire in 10 minutes.

Replies to this email are undeliverable. Call 5551234567 or write to 2026 Main St, Suite 987654.
"""


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "---------- Forwarded message ---------\nFrom: Acme Careers "
        "<careers@careers.acme.com>\nDate: 1 Jan 2026\n\n",
    ],
)
def test_template_extracts_code_from_direct_and_forwarded_email(configured, monkeypatch, prefix):
    body = prefix + SAMPLE_EMAIL
    if not prefix:
        body = "careers@careers.acme.com\n" + body
    monkeypatch.setattr(
        mailbox, "_client", lambda key: fake_client([msg("1", "Please confirm your identity", body)])
    )
    assert mailbox.wait_for_code("acme", since=mailbox.now(), interval=0) == "123456"


def test_wait_ignores_emails_sent_before_login_started(configured, monkeypatch):
    old = msg("1", "Sign in", "Acme code 111111")
    old["timestamp"] = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(mailbox, "_client", lambda key: fake_client([old]))
    monkeypatch.setattr(mailbox, "load_template", lambda c: mailbox.EmailTemplate(contains="acme", timeout=0))
    with pytest.raises(MailboxError, match="No email"):
        mailbox.wait_for_code("acme", since=mailbox.now(), interval=0)


def test_wait_does_not_reuse_an_email_from_an_earlier_login(configured, monkeypatch):
    monkeypatch.setattr(
        mailbox, "_client", lambda key: fake_client([msg("1", "Sign in", "Acme code 111111")])
    )
    assert mailbox.wait_for_code("acme", since=mailbox.now(), interval=0) == "111111"
    monkeypatch.setattr(mailbox, "load_template", lambda c: mailbox.EmailTemplate(contains="acme", timeout=0))
    with pytest.raises(MailboxError, match="No email"):  # same email again must not be accepted
        mailbox.wait_for_code("acme", since=mailbox.now(), interval=0)
