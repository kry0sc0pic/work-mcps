"""Local JSON snapshot store + diffing, so servers can report changes between runs."""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from ats_core.models import Application, Delta, StatusChange


def default_cache_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "work-mcps"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "work-mcps"


class SnapshotStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> tuple[str | None, dict[str, Application]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None, {}
        apps = {a["id"]: Application.model_validate(a) for a in raw["applications"]}
        return raw["taken_at"], apps

    def save(self, apps: list[Application]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "applications": [a.model_dump(mode="json") for a in apps],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def diff(taken_at: str | None, old: dict[str, Application], current: list[Application]) -> Delta:
    baseline = taken_at is None
    cur = {a.id: a for a in current}
    new = [a for i, a in cur.items() if i not in old]
    changed = [
        StatusChange(application=a, previous_status=old[i].status)
        for i, a in cur.items()
        if i in old and old[i].status != a.status
    ]
    removed = [a for i, a in old.items() if i not in cur]
    unchanged = len(cur) - len(new) - len(changed)
    return Delta(
        baseline=baseline,
        previous_snapshot_at=taken_at,
        new=new,
        status_changed=changed,
        removed=removed,
        unchanged_count=unchanged,
    )
