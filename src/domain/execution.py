from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_serializer


RunStatus = Literal["running", "cancel_requested", "completed", "awaiting_approval", "failed", "cancelled", "timed_out"]


class ExecutionCancelled(Exception):
    pass


class ExecutionTimedOut(Exception):
    pass


class RunConflict(Exception):
    pass


class RunNotFound(Exception):
    pass


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    turn_id: UUID
    parent_run_id: UUID | None
    thread_id: UUID | None
    employee_id: UUID
    initiated_by_employee_id: UUID
    skill_id: str
    skill_version: str
    status: RunStatus
    model_calls: int
    tool_calls: int
    error_type: str | None
    approval_id: UUID | None
    cancel_requested: bool
    started_at: datetime
    deadline_at: datetime
    finished_at: datetime | None

    @field_serializer("started_at", "deadline_at", "finished_at", when_used="json")
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value is not None else None
