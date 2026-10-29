from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_serializer

from src.domain.quotes import QuoteDraft


ApprovalStatus = Literal["pending", "approved", "rejected", "completed", "failed", "cancelled", "expired"]


class ApprovalTask(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    approval_id: UUID
    thread_id: UUID | None
    requested_by_employee_id: UUID
    requested_by_username: str
    status: ApprovalStatus
    quote_draft: QuoteDraft
    decision: Literal["approve", "reject", "cancel"] | None
    decided_by_employee_id: UUID | None
    decided_at: datetime | None
    review_note: str
    quote_id: UUID | None
    failure_reason: str | None
    resume_attempts: int
    last_resume_error: str | None
    workflow_completed: bool
    expires_at: datetime
    created_at: datetime

    @field_serializer("decided_at", "expires_at", "created_at", when_used="json")
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value is not None else None
