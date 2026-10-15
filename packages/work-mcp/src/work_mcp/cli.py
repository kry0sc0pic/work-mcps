"""`work-mcp`: set up the candidate-side ATS MCP servers."""

import getpass
import json
import os
import secrets
import shutil
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Annotated

import questionary
import typer
from ats_core import Credential, CredentialStore, CredentialStoreError
from ats_core.mailbox import load_mailbox
from rich.console import Console
from rich.table import Table

from work_mcp.clients import Client, ServerSpec, all_clients
from work_mcp.importer import CsvFormatError, ImportPlan, apply_plan, build_plan
from work_mcp.platforms import PLATFORMS, Platform

app = typer.Typer(help="Set up MCP servers for your job applications.", no_args_is_help=True)
console = Console()

EXPORT_HELP = (
    "Export a CSV from your password manager:\n"
    "  • Apple Passwords: File → Export All Passwords… (exports everything at once)\n"
    "  • Chrome: Settings → Passwords → Export passwords\n"
    "  • 1Password / Bitwarden: export as CSV\n"
    "The file holds every password you own in plain text. Only rows for the platforms you pick are "
    "used, and nothing is printed."
)
PRIVACY_NOTE = (
    "Your passwords are read on this machine and saved to your system keychain. "
    "They are never uploaded anywhere."
)
STATUS_STYLE = {
    "new": "green",
    "update": "yellow",
    "lookup": "cyan",
    "unchanged": "dim",
    "conflict": "red",
    "skipped": "red",
}


def read_password(prompt: str) -> str:
    return getpass.getpass(prompt)


def repo_root() -> Path:
    for p in Path(__file__).resolve().parents:
        pyproject = p / "pyproject.toml"
        if pyproject.exists() and "workspace" in tomllib.loads(pyproject.read_text()).get("tool", {}).get(
            "uv", {}
        ):
            return p
    return Path.cwd()


def server_spec(platform: Platform) -> ServerSpec:
    # Absolute uv path: editors launched from the Dock or Start menu often lack uv on their PATH.
    uv = shutil.which("uv") or "uv"
    return ServerSpec(platform.id, uv, ["run", "--project", str(repo_root()), platform.server_script])


def pick_platforms(ids: list[str] | None) -> list[Platform]:
    unknown = [i for i in ids or [] if i not in PLATFORMS]
    if unknown:
        raise typer.BadParameter(
            f"Unknown platform {', '.join(unknown)}. Choose from: {', '.join(PLATFORMS)}"
        )
    return [PLATFORMS[i] for i in ids] if ids else list(PLATFORMS.values())


def show_plan(plan: ImportPlan) -> None:
    table = Table("Platform", "Account", "Username", "Action", "Note")
    for it in plan.items:
        style = STATUS_STYLE[it.status]
        table.add_row(
            it.platform.label,
            it.ref.label if it.ref else it.source_url[:60],
            it.credential.username if it.credential else "",
            f"[{style}]{it.status}[/{style}]",
            it.detail,
        )
    console.print(table)
    console.print(f"[dim]{plan.rows_total} rows read, {plan.rows_ignored} unrelated rows ignored.[/dim]")


def choose_platforms(plan: ImportPlan) -> list[str]:
    """Let the user pick which platforms' passwords to import: one `Platform: N passwords` row each."""
    counts = Counter(it.platform.id for it in plan.writable())
    labels = {p.id: p.label for p in PLATFORMS.values()}
    if not counts:
        return []
    if not sys.stdin.isatty():  # piped input (scripts, tests): no TUI, just confirm everything
        total = sum(counts.values())
        return list(counts) if typer.confirm(f"Save {total} login(s) to the keychain?", default=True) else []
    choices = [
        questionary.Choice(f"{labels[pid]}: {n} password{'s' if n != 1 else ''}", value=pid, checked=True)
        for pid, n in counts.items()
    ]
    picked = questionary.checkbox(
        "Select what to import (space to toggle, enter to confirm)", choices=choices
    ).ask()
    return picked or []


def load_plan(path: Path, platforms: list[Platform]) -> ImportPlan:
    try:
        return build_plan(path, [p.id for p in platforms])
    except (OSError, CsvFormatError) as e:
        console.print(f"[red]Can't read {path}: {e}[/red]")
        raise typer.Exit(1) from e


def run_import(path: Path, platforms: list[Platform], *, yes: bool, delete: bool | None) -> int:
    console.print(f"[dim]{PRIVACY_NOTE}[/dim]")
    plan = load_plan(path, platforms)
    if not plan.items:
        console.print("No matching logins found in that file.")
        return 0
    skipped = sum(1 for i in plan.items if i.status in ("skipped", "conflict"))
    unchanged = sum(1 for i in plan.items if i.status == "unchanged")
    for n, what in ((skipped, "skipped or conflicting"), (unchanged, "already saved")):
        if n:
            console.print(f"[dim]{n} row(s) {what}. Run with --dry-run for details.[/dim]")
    selected = list({i.platform.id for i in plan.writable()}) if yes else choose_platforms(plan)
    saved = apply_plan(plan, set(selected)) if selected else 0
    for it in plan.items:
        if it.status == "skipped" and it.source_url and it.credential and selected:
            console.print(f"[yellow]Skipped {it.platform.key_for(it.source_url)}: {it.detail}[/yellow]")
    if saved:
        console.print(f"[green]Saved {saved} login(s) to the keychain.[/green]")
    elif plan.writable():
        console.print("Nothing imported.")
    else:
        console.print("Nothing new to import.")
    if delete is None:
        delete = typer.confirm(f"Delete {path.name} now? It contains plaintext passwords.", default=True)
    if delete:
        path.unlink(missing_ok=True)
        console.print("Deleted. (Deletion isn't a secure erase; FileVault protects the disk.)")
    return saved


def clean_path(raw: str) -> Path:
    """A path typed or dragged into the terminal: strip quotes, and shell escapes except on Windows."""
    raw = raw.strip().strip("'\"")
    if sys.platform != "win32":
        raw = raw.replace("\\ ", " ")
    return Path(raw).expanduser()


def keychain_works() -> bool:
    try:
        CredentialStore("workday").get("__probe__")
    except CredentialStoreError:
        return False
    return True


def headless_setup(mode: str | None, *, yes: bool) -> bool:
    """Configure a file-based keyring for machines with no OS keychain (Linux servers). Opt-in only."""
    from ats_core.credentials import default_config_dir, keyring_settings_path

    if mode is None:
        console.print(
            "  1. Encrypted file (recommended): logins encrypted with a random key kept beside them"
        )
        console.print("  2. Plain-text file: readable JSON, owner-only permissions")
        mode = {"1": "encrypted", "2": "plaintext"}.get(typer.prompt("Choice", default="1"), "")
    if mode not in ("encrypted", "plaintext"):
        raise typer.BadParameter("mode must be 'encrypted' or 'plaintext'")
    if not sys.platform.startswith("linux"):
        console.print(
            "[red]Headless storage is only offered on Linux; macOS and Windows have a real keychain.[/red]"
        )
        return False
    config = default_config_dir()
    config.mkdir(parents=True, exist_ok=True)
    if mode == "plaintext":
        console.print(
            "[yellow]Logins will be stored unencrypted in "
            f"{config / 'credentials.json'} (mode 600). Anyone who can read that file, or a backup of it, "
            "can read your passwords.[/yellow]"
        )
        if not (yes or typer.confirm("Use plain-text storage?", default=False)):
            return False
        settings = {"backend": "plaintext"}
    else:
        key = config / "keyring.key"
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secrets.token_urlsafe(32))
        settings = {"backend": "file", "password_file": str(key)}
        console.print(
            f"Logins go to {config / 'credentials.enc'}, encrypted with the random key in {key}. "
            "The key lives on this machine, so this protects copies and backups of the data file, "
            "not someone who can read both files."
        )
    keyring_settings_path().write_text(json.dumps(settings, indent=2), encoding="utf-8")
    console.print(
        "[green]Headless storage is on.[/green] Logins saved in your OS keychain earlier are not moved: "
        "re-add or re-import them."
    )
    return True


def choose_clients(ids: list[str] | None, *, yes: bool) -> list[Client]:
    """Which AI clients to add the servers to: those named, else a picker with detected ones ticked."""
    known = all_clients()
    if ids:
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise typer.BadParameter(f"Unknown client {', '.join(unknown)}. Known: {', '.join(known)}")
        return [known[i] for i in ids]
    detected = [c for c in known.values() if c.detected()]
    if yes or not sys.stdin.isatty():  # no TUI: take whatever is installed
        return detected
    choices = [
        questionary.Choice(
            f"{c.label}{' (detected)' if c in detected else ''}", value=c.id, checked=c in detected
        )
        for c in known.values()
    ]
    picked = questionary.checkbox(
        "Add the servers to which AI clients? (space to toggle, enter to confirm)", choices=choices
    ).ask()
    return [known[i] for i in picked or []]


def register(platforms: list[Platform], clients: list[Client]) -> None:
    if not clients:
        console.print("No supported AI client selected or found. To add the servers yourself:")
        for c in all_clients().values():
            for p in platforms:
                console.print(f"  {c.label}: {c.manual(server_spec(p))}")
        return
    for c in clients:
        for p in platforms:
            out = c.install(server_spec(p))
            if out.status == "added":
                console.print(f"[green]{c.label}: registered {p.id}.[/green]")
            elif out.status == "exists":
                console.print(f"{c.label}: {p.id} is already registered.")
            else:
                console.print(f"[red]{c.label}: failed[/red] ({out.message}). Do it by hand:")
                console.print(f"  {c.manual(server_spec(p))}")


def add_manually(platforms: list[Platform]) -> None:
    console.print("Enter a careers-site URL for each account (blank to finish).")
    while url := typer.prompt("URL", default="", show_default=False).strip():
        platform = next((p for p in platforms if p.owns(url)), None)
        if platform is None:
            console.print(
                f"[red]That doesn't look like a {' / '.join(p.label for p in platforms)} URL.[/red]"
            )
            continue
        try:
            ref = platform.parse(url, source="wizard")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            continue
        username = typer.prompt(f"Username/email for {ref.label}")
        if platform.uses_password:
            platform.store().save(
                ref.key, Credential(username=username, password=read_password("Password: ")), meta=ref.meta
            )
        else:
            platform.store().save_account(ref.key, username, ref.meta)
            if load_mailbox() is None:
                console.print(
                    "[yellow]This site emails you a login code. Run `work-mcp mailbox` so the server "
                    "can read it.[/yellow]"
                )
        console.print(f"[green]Saved {ref.label}.[/green]")


def show_accounts(platforms: list[Platform]) -> None:
    table = Table("Platform", "Company", "Account", "Username", "Added", "Source")
    for p in platforms:
        for key, meta in p.store().list().items():
            table.add_row(
                p.label, meta.get("company", ""), key, meta.get("username", ""),
                meta.get("added_at", ""), meta.get("source", ""),
            )  # fmt: skip
    console.print(table if table.row_count else "No accounts saved yet.")


@app.command()
def setup(
    yes: Annotated[bool, typer.Option("--yes", help="Accept defaults without asking.")] = False,
) -> None:
    """Interactive wizard: choose platforms, add logins, register the servers with your AI clients."""
    console.rule("[bold]work-mcp setup")
    if sys.platform.startswith("linux") and not keychain_works():
        console.print("[yellow]No usable system keychain was found on this machine.[/yellow]")
        if yes or typer.confirm("Set up headless storage instead?", default=True):
            headless_setup(None if not yes else "encrypted", yes=yes)
    platforms = list(PLATFORMS.values())
    if len(platforms) > 1:
        for i, p in enumerate(platforms, 1):
            console.print(f"  {i}. {p.label}")
        raw = typer.prompt("Which platforms? (numbers, comma-separated)", default="all")
        if raw.strip().lower() != "all":
            chosen = {int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()}
            platforms = [p for i, p in enumerate(platforms, 1) if i in chosen]
    console.print(f"Platforms: {', '.join(p.label for p in platforms)}\n")

    console.print("How do you want to add your logins?")
    console.print("  1. Import from a password-manager CSV export")
    console.print("  2. Type them in one at a time")
    console.print("  3. Skip (use what's already saved)")
    choice = typer.prompt("Choice", default="1" if not yes else "3")
    if choice == "1":
        console.print(f"\n{EXPORT_HELP}\n")
        raw_path = typer.prompt("Path to the CSV (drag the file here)")
        run_import(clean_path(raw_path), platforms, yes=yes, delete=None)
    elif choice == "2":
        add_manually(platforms)

    console.print()
    show_accounts(platforms)
    console.print()
    register(platforms, choose_clients(None, yes=yes))
    console.print(
        "\n[bold green]Done.[/bold green] Restart your AI client, then ask about your applications."
    )


@app.command("import")
def import_csv(
    path: Annotated[Path, typer.Argument(help="CSV exported from a password manager.")],
    platform: Annotated[
        list[str] | None, typer.Option("--platform", "-p", help="Limit to a platform.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be imported, save nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before saving.")] = False,
    delete: Annotated[bool | None, typer.Option("--delete/--keep", help="Delete the CSV afterwards.")] = None,
) -> None:
    """Import matching logins from a password-manager CSV export."""
    platforms = pick_platforms(platform)
    if dry_run:
        console.print(f"[dim]{PRIVACY_NOTE}[/dim]")
        show_plan(load_plan(path.expanduser(), platforms))
        return
    run_import(path.expanduser(), platforms, yes=yes, delete=delete)


@app.command()
def accounts() -> None:
    """List saved accounts (never shows passwords)."""
    show_accounts(list(PLATFORMS.values()))


@app.command("add-oracle")
def add_oracle(
    url: Annotated[str, typer.Argument(help="The careers sign-in page URL (contains /sites/<SITE>/).")],
    email: Annotated[str, typer.Argument(help="The email address you sign in with.")],
    company: Annotated[str | None, typer.Option("--company", help="Display name.")] = None,
) -> None:
    """Add an Oracle Recruiting Cloud careers site (email-code login, no password)."""
    platform = PLATFORMS["oracle"]
    try:
        ref = platform.parse(url, company, source="cli")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    platform.store().save_account(ref.key, email, ref.meta)
    console.print(f"[green]Saved {ref.label}.[/green] The first refresh emails a login code to {email}.")
    if load_mailbox() is None:
        console.print("[yellow]Run `work-mcp mailbox` so the server can read that code.[/yellow]")


@app.command("add-greenhouse")
def add_greenhouse(
    email: Annotated[str, typer.Argument(help="The email address your MyGreenhouse account uses.")],
) -> None:
    """Add your MyGreenhouse account (email-code login, no password)."""
    platform = PLATFORMS["greenhouse"]
    ref = platform.parse(f"https://{platform.HOST}", source="cli")  # type: ignore[attr-defined]
    platform.store().save_account(ref.key, email, ref.meta)
    console.print(f"[green]Saved {ref.label}.[/green] The first refresh emails a login code to {email}.")
    if load_mailbox() is None:
        console.print("[yellow]Run `work-mcp mailbox` so the server can read that code.[/yellow]")


@app.command()
def headless(
    mode: Annotated[str | None, typer.Option("--mode", help="encrypted or plaintext")] = None,
    off: Annotated[bool, typer.Option("--off", help="Go back to the OS keychain.")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Linux without a desktop keychain: store logins in an encrypted (or plain-text) file instead."""
    from ats_core.credentials import keyring_settings_path

    if off:
        keyring_settings_path().unlink(missing_ok=True)
        console.print("Back to the OS keychain. Logins saved in the file store stay where they are.")
        return
    if not headless_setup(mode, yes=yes):
        raise typer.Exit(1)


@app.command()
def mailbox(
    inbox: Annotated[str | None, typer.Option("--inbox", help="AgentMail inbox id (its address).")] = None,
    remove: Annotated[bool, typer.Option("--remove", help="Forget the saved key and inbox.")] = False,
) -> None:
    """Save the AgentMail key and inbox used to read login codes that sites email to you."""
    from ats_core.mailbox import MailboxError, check_mailbox, delete_mailbox, save_mailbox

    if remove:
        delete_mailbox()
        console.print("Mailbox settings removed.")
        return
    inbox_id = inbox or typer.prompt("AgentMail inbox id (the inbox's email address)")
    key = read_password("AgentMail API key (input hidden): ")
    try:
        check_mailbox(key, inbox_id)
    except MailboxError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    save_mailbox(key, inbox_id)
    console.print("Saved: key in your keychain, inbox id in the config folder.")


@app.command("register")
def register_cmd(
    platform: Annotated[list[str] | None, typer.Option("--platform", "-p")] = None,
    client: Annotated[
        list[str] | None, typer.Option("--client", "-c", help="claude, codex, cursor, grok, hermes, openclaw")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Use every detected client without asking.")] = False,
) -> None:
    """Register the MCP servers with your AI clients (Claude Code, Codex, Cursor, Grok, Hermes, OpenClaw)."""
    register(pick_platforms(platform), choose_clients(client, yes=yes))


def main() -> None:
    try:
        app()
    except CredentialStoreError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from e
