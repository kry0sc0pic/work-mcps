"""`work-mcp`: set up the candidate-side ATS MCP servers."""

import getpass
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Annotated

import typer
from ats_core import Credential
from rich.console import Console
from rich.table import Table

from work_mcp.importer import CsvFormatError, ImportPlan, apply_plan, build_plan
from work_mcp.platforms import PLATFORMS, Platform

app = typer.Typer(help="Set up MCP servers for your job applications.", no_args_is_help=True)
console = Console()

EXPORT_HELP = (
    "Export a CSV from your password manager:\n"
    "  • Apple Passwords: File → Export All Passwords…\n"
    "  • Chrome: Settings → Passwords → Export passwords\n"
    "  • 1Password / Bitwarden: export as CSV\n"
    "The file holds every password you own in plain text. Only rows for the platforms you pick are "
    "used, and nothing is printed."
)
STATUS_STYLE = {"new": "green", "update": "yellow", "unchanged": "dim", "conflict": "red", "skipped": "red"}


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


def mcp_add_command(platform: Platform) -> list[str]:
    return [
        "claude", "mcp", "add", "--scope", "user", platform.id,
        "--", "uv", "run", "--project", str(repo_root()), platform.server_script,
    ]  # fmt: skip


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


def run_import(path: Path, platforms: list[Platform], *, yes: bool, delete: bool | None) -> int:
    try:
        plan = build_plan(path, [p.id for p in platforms])
    except (OSError, CsvFormatError) as e:
        console.print(f"[red]Can't read {path}: {e}[/red]")
        raise typer.Exit(1) from e
    if not plan.items:
        console.print("No matching logins found in that file.")
        return 0
    show_plan(plan)
    todo = plan.writable()
    saved = 0
    if todo and (yes or typer.confirm(f"Save {len(todo)} login(s) to the keychain?", default=True)):
        saved = apply_plan(plan)
        console.print(f"[green]Saved {saved} login(s) to the keychain.[/green]")
    if delete is None:
        delete = typer.confirm(f"Delete {path.name} now? It contains plaintext passwords.", default=True)
    if delete:
        path.unlink(missing_ok=True)
        console.print("Deleted. (Deletion isn't a secure erase; FileVault protects the disk.)")
    return saved


def register(platforms: list[Platform], *, yes: bool) -> None:
    if not shutil.which("claude"):
        console.print("Claude Code CLI not found. Register manually with:")
        for p in platforms:
            console.print(f"  {' '.join(mcp_add_command(p))}")
        return
    for p in platforms:
        cmd = mcp_add_command(p)
        console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
        if yes or typer.confirm(f"Register the {p.label} server with Claude Code?", default=True):
            r = subprocess.run(cmd, capture_output=True, text=True)
            msg = (r.stdout + r.stderr).strip()
            if r.returncode == 0:
                console.print(f"[green]Registered {p.id}.[/green]")
            elif "already exists" in msg:
                console.print(f"{p.id} is already registered.")
            else:
                console.print(f"[red]Failed:[/red] {msg}")


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
        platform.store().save(
            ref.key, Credential(username=username, password=read_password("Password: ")), meta=ref.meta
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
    """Interactive wizard: choose platforms, add logins, register the servers."""
    console.rule("[bold]work-mcp setup")
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
        raw_path = typer.prompt("Path to the CSV (drag the file here)").strip().strip("'\"")
        run_import(Path(raw_path.replace("\\ ", " ")).expanduser(), platforms, yes=yes, delete=None)
    elif choice == "2":
        add_manually(platforms)

    console.print()
    show_accounts(platforms)
    console.print()
    register(platforms, yes=yes)
    console.print("\n[bold green]Done.[/bold green] Restart Claude Code, then ask about your applications.")


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
        show_plan(build_plan(path.expanduser(), [p.id for p in platforms]))
        return
    run_import(path.expanduser(), platforms, yes=yes, delete=delete)


@app.command()
def accounts() -> None:
    """List saved accounts (never shows passwords)."""
    show_accounts(list(PLATFORMS.values()))


@app.command("register")
def register_cmd(
    platform: Annotated[list[str] | None, typer.Option("--platform", "-p")] = None,
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    """Register the MCP servers with Claude Code."""
    register(pick_platforms(platform), yes=yes)
