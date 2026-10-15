"""SQLite cache of every account's applications on one platform, plus status history.

One file per platform (stdlib sqlite3, no server). Rows are keyed by (account, application id), so a
single database answers "all my applications", "just Hooli", or "when did this change?".
"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from ats_core.models import Application

_SCHEMA = """
create table if not exists applications (
    account text not null, id text not null, job_id text, title text, status text, state text,
    applied_on text, can_withdraw integer, is_draft integer, has_pending_tasks integer,
    first_seen text, last_seen text, company text,
    primary key (account, id)
);
create table if not exists status_history (
    account text not null, id text not null, title text, status text, seen_at text not null
);
create index if not exists status_history_idx on status_history (account, id);
create table if not exists syncs (account text primary key, synced_at text not null);
"""


class StatusEvent(BaseModel):
    """A status we observed. `seen_at` is when we noticed it, not when the employer changed it."""

    account: str
    application_id: str
    title: str
    status: str
    seen_at: str


def _now() -> datetime:
    return datetime.now(UTC)


def is_fresh(synced_at: datetime | None, ttl: timedelta, now: datetime | None = None) -> bool:
    return synced_at is not None and (now or _now()) - synced_at < ttl


class ApplicationCache:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        if "company" not in {r[1] for r in conn.execute("pragma table_info(applications)")}:
            conn.execute("alter table applications add column company text")  # caches from before it existed
        return conn

    def synced_at(self, account: str) -> datetime | None:
        with closing(self._connect()) as conn:
            row = conn.execute("select synced_at from syncs where account = ?", (account,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def replace(self, account: str, apps: list[Application], now: datetime | None = None) -> None:
        """Make the cache for `account` match `apps`, recording status changes in the history."""
        stamp = (now or _now()).isoformat(timespec="seconds")
        with closing(self._connect()) as conn, conn:
            known = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    "select id, status, first_seen from applications where account = ?", (account,)
                )
            }
            for a in apps:
                prev = known.get(a.id)
                if prev is None or prev[0] != a.status:
                    conn.execute(
                        "insert into status_history values (?, ?, ?, ?, ?)",
                        (account, a.id, a.title, a.status, stamp),
                    )
                conn.execute(
                    "insert or replace into applications (account, id, job_id, title, status, state,"
                    " applied_on, can_withdraw, is_draft, has_pending_tasks, first_seen, last_seen, company)"
                    " values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        account, a.id, a.job_id, a.title, a.status, a.state,
                        a.applied_on.isoformat() if a.applied_on else None,
                        int(a.can_withdraw), int(a.is_draft), int(a.has_pending_tasks),
                        prev[1] if prev else stamp, stamp, a.company,
                    ),
                )  # fmt: skip
            keep = [a.id for a in apps]
            conn.execute(
                f"delete from applications where account = ? and id not in ({','.join('?' * len(keep))})",
                (account, *keep),
            )
            conn.execute("insert or replace into syncs values (?, ?)", (account, stamp))

    def load(self, accounts: list[str] | None = None) -> list[Application]:
        sql, args = "select * from applications", []
        if accounts is not None:
            sql += f" where account in ({','.join('?' * len(accounts))})"
            args = accounts
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, args).fetchall()
        return [
            Application(
                id=r["id"], account=r["account"], job_id=r["job_id"] or "", title=r["title"] or "",
                status=r["status"] or "", state=r["state"], company=r["company"] or "",
                applied_on=date.fromisoformat(r["applied_on"]) if r["applied_on"] else None,
                can_withdraw=bool(r["can_withdraw"]), is_draft=bool(r["is_draft"]),
                has_pending_tasks=bool(r["has_pending_tasks"]),
            )
            for r in rows
        ]  # fmt: skip

    def history(
        self, accounts: list[str] | None = None, application_id: str | None = None, limit: int = 200
    ) -> list[StatusEvent]:
        where, args = [], []
        if accounts is not None:
            where.append(f"account in ({','.join('?' * len(accounts))})")
            args += accounts
        if application_id:
            where.append("id = ?")
            args.append(application_id)
        sql = "select account, id, title, status, seen_at from status_history"
        if where:
            sql += " where " + " and ".join(where)
        sql += " order by seen_at desc, rowid desc limit ?"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, (*args, limit)).fetchall()
        return [
            StatusEvent(account=r[0], application_id=r[1], title=r[2], status=r[3], seen_at=r[4])
            for r in rows
        ]

    def clear(self, account: str) -> None:
        with closing(self._connect()) as conn, conn:
            for table in ("applications", "status_history", "syncs"):
                conn.execute(f"delete from {table} where account = ?", (account,))
