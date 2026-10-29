from datetime import datetime, timezone
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_serializer, model_validator

from src.domain.quotes import QuoteDraft
from src.domain.approvals import ApprovalTask
from src.domain.reports import ReportEvidence
from src.domain.report_analysis import VehicleReportAnalysis
from src.domain.tasks import TaskView
from src.domain.planning import PlanView
from src.domain.memory import MemoryView
from src.domain.web_search import WebSource, WebSearchStatus

class HealthResponse(BaseModel):
    status: str
    stage: str
    version: str
    mode: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: UUID | None = None
    skill_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]


class Vehicle(BaseModel):
    vehicle_id: str
    title: str
    price_cny: int = Field(ge=0)
    mileage_km: int = Field(ge=0)
    is_demo: bool


class VehicleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^[^\x00]+$"),
    ]
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00]+$"),
    ]
    # 与 PostgreSQL INTEGER 列的范围保持一致，金额单位为整数元。
    price_cny: int = Field(strict=True, ge=0, le=2_147_483_647)
    mileage_km: int = Field(strict=True, ge=0, le=2_147_483_647)


class VehicleCreated(Vehicle):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class QuoteSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^[^\x00]+$"),
    ]
    discount_cny: int = Field(default=0, strict=True, ge=0, le=2_147_483_647)
    expected_price_cny: int = Field(
        strict=True, ge=0, le=2_147_483_647,
        description="预览报价时的车辆售价；保存前与当前库存售价核对。",
    )


class QuoteConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


class QuoteSaved(QuoteDraft):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    quote_id: UUID
    created_at: datetime
    status: Literal["draft", "confirmed"] = "draft"
    requires_confirmation: bool = True
    confirmed_at: datetime | None = None
    confirmed_by_employee_id: UUID | None = None
    created_by_employee_id: UUID | None = None

    @model_validator(mode="after")
    def validate_confirmation_state(self) -> Self:
        if self.status == "draft":
            valid = self.requires_confirmation and self.confirmed_at is None and self.confirmed_by_employee_id is None
        else:
            valid = not self.requires_confirmation and self.confirmed_at is not None
        if not valid:
            raise ValueError("报价状态与确认标记、确认时间不一致。")
        return self

    @field_serializer("generated_at", "created_at", "confirmed_at", when_used="json")
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value is not None else None


class ChatResponse(BaseModel):
    run_id: UUID
    thread_id: UUID
    reply: str
    vehicle: Vehicle | None = None
    vehicles: list[Vehicle] = Field(default_factory=list)
    has_more: bool = False
    quote_draft: QuoteDraft | None = None
    status: Literal["completed", "awaiting_approval", "cancelled"] = "completed"
    skill_id: str | None = None
    skill_version: str | None = None
    approval: ApprovalTask | None = None
    quote_id: UUID | None = None
    report_sources: list[ReportEvidence] = Field(default_factory=list)
    report_analysis: VehicleReportAnalysis | None = None
    plan: PlanView | None = None
    memory_proposals: list[MemoryView] = Field(default_factory=list)
    web_sources: list[WebSource] = Field(default_factory=list)
    web_search_status: WebSearchStatus | None = None


class TaskResultResponse(BaseModel):
    task: TaskView
    result: ChatResponse | None = None


class EmployeeLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=32,
                               pattern=r"^[A-Za-z][A-Za-z0-9_.-]{2,31}$"),
    ]
    password: SecretStr = Field(min_length=1, max_length=128)


class EmployeeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    employee_id: UUID
    username: str
    role: Literal["sales", "manager"]


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    employee: EmployeeResponse


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] = "新会话"


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    thread_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_timestamp(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()


class ConversationList(BaseModel):
    conversations: list[ConversationResponse]
    has_more: bool


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str | list[str | dict]
    tool_calls: list[dict] = Field(default_factory=list)
    tool_name: str | None = None
    tool_call_id: str | None = None


class ConversationHistory(ConversationResponse):
    messages: list[ConversationMessage]
    pending_approval_id: UUID | None = None


class ApprovalReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class ApprovalResolution(BaseModel):
    run_id: UUID
    approval: ApprovalTask
    execution_status: Literal["completed", "failed", "waiting"]


class ApprovalList(BaseModel):
    approvals: list[ApprovalTask]
    has_more: bool
