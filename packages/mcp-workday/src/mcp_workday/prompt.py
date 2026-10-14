"""Native OS dialogs for collecting a password without it passing through the chat or the model.

macOS uses `osascript`. Windows and Linux use a Tk dialog (stdlib `tkinter`) in a subprocess, which
needs a display. Values go in as argv, never interpolated into script source.
"""

import asyncio
import subprocess
import sys

from ats_core import Credential

_APPLESCRIPT = [
    "on run argv",
    "set r to display dialog (item 1 of argv) default answer (item 2 of argv) "
    'with title "work-mcps" with icon note buttons {"Cancel", "OK"} default button "OK" '
    'hidden answer (item 3 of argv is "hidden")',
    "return text returned of r",
    "end run",
]

# Exit 0 = answered (printed), 1 = cancelled, 2 = no tkinter or no display.
_TK_SCRIPT = """
import sys
try:
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk()
except Exception:
    sys.exit(2)
root.withdraw()
root.attributes("-topmost", True)
message, default, mode = sys.argv[1:4]
answer = simpledialog.askstring("work-mcps", message, initialvalue=default,
                                show="*" if mode == "hidden" else None, parent=root)
if answer is None:
    sys.exit(1)
sys.stdout.write(answer)
"""
DIALOG_TIMEOUT_S = 300
NO_DIALOG_HELP = (
    "No graphical password dialog is available here (it needs tkinter and a display). "
    "Run `uv run mcp-workday-login <url> <email>` in a terminal instead."
)


class PromptCancelledError(RuntimeError):
    pass


class PromptUnavailableError(RuntimeError):
    pass


def _command(message: str, default: str, hidden: bool) -> list[str]:
    mode = "hidden" if hidden else "visible"
    if sys.platform == "darwin":
        cmd = ["osascript"]
        for line in _APPLESCRIPT:
            cmd += ["-e", line]
        return [*cmd, message, default, mode]
    return [sys.executable, "-c", _TK_SCRIPT, message, default, mode]


def _dialog(message: str, default: str = "", *, hidden: bool = False) -> str:
    cmd = _command(message, default, hidden)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=DIALOG_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise PromptCancelledError("The password dialog timed out.") from e
    except FileNotFoundError as e:
        raise PromptUnavailableError(NO_DIALOG_HELP) from e
    if out.returncode == 2 and sys.platform != "darwin":
        raise PromptUnavailableError(NO_DIALOG_HELP)
    if out.returncode != 0:  # osascript -128 / Tk cancel
        raise PromptCancelledError("The dialog was cancelled.")
    return out.stdout.rstrip("\n")


def _ask(site_label: str, default_username: str | None) -> Credential:
    username = default_username or _dialog(f"Username or email for {site_label}:")
    password = _dialog(f"Password for {username} on {site_label}:", hidden=True)
    if not username.strip() or not password:
        raise PromptCancelledError("Username and password are both required.")
    return Credential(username=username.strip(), password=password)


async def prompt_credentials(site_label: str, default_username: str | None = None) -> Credential:
    """Ask the user for credentials in a native dialog."""
    return await asyncio.to_thread(_ask, site_label, default_username)
