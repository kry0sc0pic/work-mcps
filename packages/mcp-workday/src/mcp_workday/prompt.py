"""Native OS dialogs for collecting a password without it passing through the chat or the model."""

import asyncio
import subprocess
import sys

from ats_core import Credential

_SCRIPT = [
    "on run argv",
    "set r to display dialog (item 1 of argv) default answer (item 2 of argv) "
    'with title "work-mcps" with icon note buttons {"Cancel", "OK"} default button "OK" '
    'hidden answer (item 3 of argv is "hidden")',
    "return text returned of r",
    "end run",
]
DIALOG_TIMEOUT_S = 300


class PromptCancelledError(RuntimeError):
    pass


def _dialog(message: str, default: str = "", *, hidden: bool = False) -> str:
    # Values are passed as argv, never interpolated into the AppleScript source.
    cmd = ["osascript"]
    for line in _SCRIPT:
        cmd += ["-e", line]
    cmd += [message, default, "hidden" if hidden else "visible"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=DIALOG_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise PromptCancelledError("The password dialog timed out.") from e
    if out.returncode != 0:  # -128 = user cancelled
        raise PromptCancelledError("The dialog was cancelled.")
    return out.stdout.rstrip("\n")


def _ask(site_label: str, default_username: str | None) -> Credential:
    username = default_username or _dialog(f"Username or email for {site_label}:")
    password = _dialog(f"Password for {username} on {site_label}:", hidden=True)
    if not username.strip() or not password:
        raise PromptCancelledError("Username and password are both required.")
    return Credential(username=username.strip(), password=password)


async def prompt_credentials(site_label: str, default_username: str | None = None) -> Credential:
    """Ask the user for credentials in a native dialog (macOS only)."""
    if sys.platform != "darwin":
        raise RuntimeError(
            "Native password dialogs are only implemented on macOS. "
            "Run `uv run mcp-workday-login <url> <email>` in a terminal instead."
        )
    return await asyncio.to_thread(_ask, site_label, default_username)
