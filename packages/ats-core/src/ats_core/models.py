"""Platform-agnostic candidate-side models. Each server maps its ATS into these."""

from datetime import date
from typing import Literal

from pydantic import BaseModel


class Job(BaseModel):
    id: str
    title: str
    company: str
    location: str | None = None
    url: str | None = None
    description: str | None = None


class Application(BaseModel):
    id: str
    account: str = ""
    company: str = ""  # display name from the account index; filled in at query time, not cached
    job_id: str
    title: str
    status: str
    state: Literal["active", "inactive"]
    applied_on: date | None = None
    can_withdraw: bool = False
    is_draft: bool = False
    has_pending_tasks: bool = False


class StatusChange(BaseModel):
    application: Application
    previous_status: str


class Delta(BaseModel):
    """What changed since the previous snapshot. `baseline` is true on the first run."""

    baseline: bool
    previous_snapshot_at: str | None
    new: list[Application]
    status_changed: list[StatusChange]
    removed: list[Application]
    unchanged_count: int
