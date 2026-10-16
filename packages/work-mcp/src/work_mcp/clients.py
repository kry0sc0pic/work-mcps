"""AI clients the wizard can register the MCP servers with.

Three registration styles, all verified against each client's docs:
- a CLI that writes its own config (`claude`, `codex`, `grok` take `mcp add NAME -- CMD ARGS`;
  `openclaw mcp set NAME JSON`),
- a JSON file with a top-level `mcpServers` map (Cursor: `~/.cursor/mcp.json`),
- a YAML file with a top-level `mcp_servers` map (Hermes: `~/.hermes/config.yaml`).
"""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ruamel.yaml import YAML


@dataclass(frozen=True)
class ServerSpec:
    name: str
    command: str
    args: list[str]


@dataclass(frozen=True)
class Outcome:
    status: Literal["added", "exists", "failed"]
    message: str = ""


@dataclass(frozen=True)
class Client:
    id: str
    label: str
    kind: Literal["cli", "json", "yaml"]
    binary: str | None = None  # CLI to run (cli kind) or to detect (file kinds)
    config: Path | None = None  # config file (json/yaml kinds)

    def detected(self) -> bool:
        if self.binary and shutil.which(self.binary):
            return True
        return self.config is not None and self.config.parent.exists()

    def argv(self, spec: ServerSpec) -> list[str]:
        """Command line for the CLI-style clients, with the bare binary name first."""
        assert self.binary
        if self.id == "openclaw":
            body = json.dumps({"command": spec.command, "args": spec.args})
            return [self.binary, "mcp", "set", spec.name, body]
        scope = ["--scope", "user"] if self.id == "claude" else []
        return [self.binary, "mcp", "add", *scope, spec.name, "--", spec.command, *spec.args]

    def manual(self, spec: ServerSpec) -> str:
        """What to run or paste if automatic registration isn't possible."""
        if self.kind == "cli":
            return " ".join(_quote(a) for a in self.argv(spec))
        entry = {"command": spec.command, "args": spec.args}
        key = "mcpServers" if self.kind == "json" else "mcp_servers"
        return f"Add under `{key}` in {self.config}: {spec.name} = {json.dumps(entry)}"

    def install(self, spec: ServerSpec) -> Outcome:
        if self.kind == "cli":
            return self._install_cli(spec)
        assert self.config is not None
        try:
            return (
                _install_json(self.config, spec) if self.kind == "json" else _install_yaml(self.config, spec)
            )
        except (OSError, ValueError) as e:
            return Outcome("failed", f"couldn't update {self.config}: {e}")

    def _install_cli(self, spec: ServerSpec) -> Outcome:
        argv = self.argv(spec)
        exe = shutil.which(argv[0])  # resolved path, so Windows .cmd shims work
        if not exe:
            return Outcome("failed", f"`{argv[0]}` not found on PATH")
        r = subprocess.run([exe, *argv[1:]], capture_output=True, text=True)
        msg = (r.stdout + r.stderr).strip()
        if r.returncode == 0:
            return Outcome("added")
        if "already" in msg.lower():
            return Outcome("exists")
        return Outcome("failed", msg)


def _quote(arg: str) -> str:
    return f"'{arg}'" if any(c in arg for c in ' {}"') else arg


def _install_json(path: Path, spec: ServerSpec) -> Outcome:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object at the top level")
    servers = data.setdefault("mcpServers", {})
    if spec.name in servers:
        return Outcome("exists")
    servers[spec.name] = {"command": spec.command, "args": spec.args}
    _write(path, json.dumps(data, indent=2) + "\n")
    return Outcome("added")


def _keyring_env() -> dict[str, str]:
    """Hermes passes MCP servers only the env it is configured with, so a Linux keyring on a
    session bus (typical on headless boxes) is invisible unless the bus address is set explicitly."""
    if sys.platform != "linux":  # a comparison (not startswith) lets pyright skip this on Windows
        return {}
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not addr and (bus := Path(f"/run/user/{os.getuid()}/bus")).exists():
        addr = f"unix:path={bus}"
    return {"DBUS_SESSION_BUS_ADDRESS": addr} if addr else {}


def _install_yaml(path: Path, spec: ServerSpec) -> Outcome:
    yaml = YAML()  # round-trip mode keeps the user's comments and ordering
    data = yaml.load(path.read_text(encoding="utf-8")) if path.exists() else None
    data = {} if data is None else data
    if not isinstance(data, dict):
        raise ValueError("expected a mapping at the top level")
    servers = data.get("mcp_servers")
    if servers is None:
        servers = data["mcp_servers"] = {}
    if spec.name in servers:
        return Outcome("exists")
    entry: dict = {"command": spec.command, "args": spec.args}
    if env := _keyring_env():
        entry["env"] = env
    servers[spec.name] = entry
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)
    os.replace(tmp, path)
    return Outcome("added")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # never leave a half-written config behind


def all_clients() -> dict[str, Client]:
    home = Path.home()
    clients = [
        Client("claude", "Claude Code", "cli", binary="claude"),
        Client("codex", "Codex", "cli", binary="codex"),
        Client("cursor", "Cursor", "json", config=home / ".cursor" / "mcp.json"),
        Client("grok", "Grok Build", "cli", binary="grok"),
        Client("hermes", "Hermes Agent", "yaml", binary="hermes", config=home / ".hermes" / "config.yaml"),
        Client("openclaw", "OpenClaw", "cli", binary="openclaw"),
    ]
    return {c.id: c for c in clients}
