from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_serializer


TaskStatus = Literal["queued", "running", "cancel_requested", "awaiting_approval", "completed", "cancelled", "failed", "timed_out", "interrupted"]
ACTIVE_TASK_STATUSES = ("queued", "running", "cancel_requested")


class TaskNotFound(Exception):
    pass


class TaskConflict(Exception):
    pass


class TaskView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: UUID
    thread_id: UUID | None
    run_id: UUID
    status: TaskStatus
    skill_id: str | None
    cancel_requested: bool
    error_code: str | None
    error_message: str | None
    event_sequence: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @field_serializer("created_at", "started_at", "finished_at", when_used="json")
    def timestamp(self, value: datetime | None):
        return value.astimezone(timezone.utc).isoformat() if value else None


class TaskEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: UUID
    sequence: int
    run_id: UUID | None
    event: str
    data: dict
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def timestamp(self, value: datetime):
        return value.astimezone(timezone.utc).isoformat()


class TaskEventPage(BaseModel):
    events: list[TaskEvent]
    next_sequence: int
    has_more: bool
    status: TaskStatus


@dataclass(frozen=True)
class TaskWorkItem:
    task_id: UUID
    thread_id: UUID
    employee_id: UUID
    message: str
    skill_id: str | None
    worker_token: UUID
    run_id: UUID | None = None
    operation: str = "chat"
    command_id: UUID | None = None
