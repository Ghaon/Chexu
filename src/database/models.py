from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.database.base import Base


class UserMemoryRecord(Base):
    __tablename__ = "user_memories"
    __table_args__ = (
        CheckConstraint("category IN ('budget', 'usage', 'preferences')", name="ck_user_memories_category"),
        CheckConstraint("status IN ('pending', 'confirmed')", name="ck_user_memories_status"),
        CheckConstraint("source IN ('user_entered', 'agent_proposed')", name="ck_user_memories_source"),
        CheckConstraint("revision > 0", name="ck_user_memories_revision"),
        CheckConstraint("(status = 'confirmed') = (confirmed_at IS NOT NULL)", name="ck_user_memories_confirmation"),
        Index("ix_user_memories_owner_status", "employee_id", "status"),
        Index("uq_user_memories_confirmed_category", "employee_id", "category", unique=True,
              postgresql_where=text("status = 'confirmed'")),
    )

    memory_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(ForeignKey("employees.employee_id", ondelete="CASCADE"), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[dict | list | str | int] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    source_quote: Mapped[str | None] = mapped_column(String(500))
    source_thread_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.thread_id", ondelete="SET NULL"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VehicleRecord(Base):
    """门店车辆的数据库记录。"""

    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("vehicle_id", name="uq_vehicles_vehicle_id"),
        CheckConstraint("price_cny >= 0", name="ck_vehicles_price_nonnegative"),
        CheckConstraint("mileage_km >= 0", name="ck_vehicles_mileage_nonnegative"),
        CheckConstraint("length(btrim(vehicle_id)) > 0", name="ck_vehicles_id_not_blank"),
        CheckConstraint("length(btrim(title)) > 0", name="ck_vehicles_title_not_blank"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    vehicle_id: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    price_cny: Mapped[int] = mapped_column(Integer, nullable=False)
    mileage_km: Mapped[int] = mapped_column(Integer, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VehicleReportRecord(Base):
    __tablename__ = "vehicle_reports"
    __table_args__ = (
        UniqueConstraint("vehicle_record_id", "sha256", name="uq_vehicle_reports_content"),
        CheckConstraint("suffix IN ('.pdf','.docx','.txt','.md')", name="ck_vehicle_reports_suffix"),
        CheckConstraint("size_bytes > 0", name="ck_vehicle_reports_size"),
        Index("ix_vehicle_reports_vehicle_created", "vehicle_record_id", "created_at"),
    )

    report_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    vehicle_record_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", name="fk_vehicle_reports_vehicle", ondelete="RESTRICT"), nullable=False,
    )
    vehicle_id: Mapped[str] = mapped_column(String(32), nullable=False)
    uploaded_by_employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.employee_id", name="fk_vehicle_reports_uploader", ondelete="RESTRICT"), nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    suffix: Mapped[str] = mapped_column(String(8), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class QuoteRecord(Base):
    """独立保存的报价快照，查询时不从库存重新拼装金额和名称。"""

    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint("listed_price_cny >= 0", name="ck_quotes_price_nonnegative"),
        CheckConstraint("discount_cny >= 0 AND discount_cny <= listed_price_cny", name="ck_quotes_discount_range"),
        CheckConstraint("quoted_price_cny = listed_price_cny - discount_cny", name="ck_quotes_price_calculation"),
        CheckConstraint("status IN ('draft', 'confirmed')", name="ck_quotes_status"),
        CheckConstraint("currency = 'CNY'", name="ck_quotes_currency"),
        CheckConstraint("NOT is_total_price", name="ck_quotes_not_total_price"),
        CheckConstraint(
            "(status = 'draft' AND requires_confirmation AND confirmed_at IS NULL) OR "
            "(status = 'confirmed' AND NOT requires_confirmation AND confirmed_at IS NOT NULL)",
            name="ck_quotes_confirmation_state",
        ),
        CheckConstraint("status = 'confirmed' OR confirmed_by_employee_id IS NULL", name="ck_quotes_confirmer_state"),
    )

    quote_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    vehicle_record_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", name="fk_quotes_vehicle", ondelete="RESTRICT"), nullable=False,
    )
    vehicle_id: Mapped[str] = mapped_column(String(32), nullable=False)
    vehicle_title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="CNY")
    listed_price_cny: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_cny: Mapped[int] = mapped_column(Integer, nullable=False)
    quoted_price_cny: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_items: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    is_total_price: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by_employee_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employees.employee_id", name="fk_quotes_confirmer", ondelete="RESTRICT"), nullable=True,
    )
    created_by_employee_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employees.employee_id", name="fk_quotes_creator", ondelete="RESTRICT"), nullable=True,
    )


class EmployeeRecord(Base):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("username", name="uq_employees_username"),
        CheckConstraint("role IN ('sales', 'manager')", name="ck_employees_role"),
        CheckConstraint("username ~ '^[a-z][a-z0-9_.-]{2,31}$'", name="ck_employees_username"),
    )

    employee_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EmployeeTokenRecord(Base):
    __tablename__ = "employee_tokens"
    __table_args__ = (CheckConstraint("length(token_hash) = 64", name="ck_employee_tokens_hash_length"),)

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.employee_id", name="fk_employee_tokens_employee", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ConversationRecord(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_employee_updated", "employee_id", "updated_at"),)

    thread_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.employee_id", name="fk_conversations_employee", ondelete="CASCADE"), nullable=False,
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LoginAttemptRecord(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (CheckConstraint("failed_attempts >= 0", name="ck_login_attempts_nonnegative"),)

    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ApprovalRecord(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("thread_id", "request_key", name="uq_approvals_request"),
        UniqueConstraint("quote_id", name="uq_approvals_quote"),
        CheckConstraint("status IN ('pending','approved','rejected','completed','failed','cancelled','expired')",
                        name="ck_approvals_status"),
        CheckConstraint("resume_attempts >= 0", name="ck_approvals_resume_attempts"),
        CheckConstraint("(status = 'completed') = (quote_id IS NOT NULL)", name="ck_approvals_quote_result"),
        CheckConstraint(
            "(status = 'pending' AND decision IS NULL AND decided_by_employee_id IS NULL AND decided_at IS NULL) OR "
            "(status IN ('approved','completed','failed') AND decision IS NOT NULL AND decision = 'approve' AND decided_by_employee_id IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status = 'rejected' AND decision IS NOT NULL AND decision = 'reject' AND decided_by_employee_id IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status = 'cancelled' AND decision IS NOT NULL AND decision = 'cancel' AND decided_by_employee_id IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status = 'expired' AND decision IS NULL AND decided_at IS NOT NULL)",
            name="ck_approvals_decision",
        ),
        Index("ix_approvals_status_created", "status", "created_at"),
    )

    approval_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    thread_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.thread_id", name="fk_approvals_conversation", ondelete="SET NULL"), nullable=True,
    )
    request_key: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.employee_id", name="fk_approvals_requester", ondelete="RESTRICT"), nullable=False,
    )
    requested_by_username: Mapped[str] = mapped_column(String(32), nullable=False)
    vehicle_record_id: Mapped[int] = mapped_column(
        ForeignKey("vehicles.id", name="fk_approvals_vehicle", ondelete="RESTRICT"), nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    quote_draft: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision: Mapped[str | None] = mapped_column(String(8), nullable=True)
    decided_by_employee_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employees.employee_id", name="fk_approvals_decider", ondelete="RESTRICT"), nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    quote_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("quotes.quote_id", name="fk_approvals_quote", ondelete="RESTRICT"), nullable=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resume_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_resume_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AgentRunRecord(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_turn", "turn_id"),
        Index("ix_agent_runs_employee_started", "employee_id", "started_at"),
        CheckConstraint("status IN ('running','cancel_requested','completed','awaiting_approval','failed','cancelled','timed_out')", name="ck_agent_runs_status"),
        CheckConstraint("model_calls >= 0 AND tool_calls >= 0", name="ck_agent_runs_counts"),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    turn_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    parent_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.run_id", name="fk_agent_runs_parent", ondelete="SET NULL"), nullable=True,
    )
    thread_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.thread_id", name="fk_agent_runs_conversation", ondelete="SET NULL"), nullable=True,
    )
    employee_id: Mapped[UUID] = mapped_column(ForeignKey("employees.employee_id", name="fk_agent_runs_owner", ondelete="RESTRICT"), nullable=False)
    initiated_by_employee_id: Mapped[UUID] = mapped_column(ForeignKey("employees.employee_id", name="fk_agent_runs_actor", ondelete="RESTRICT"), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(32), nullable=False)
    context_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="running")
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_id: Mapped[UUID | None] = mapped_column(ForeignKey("approvals.approval_id", name="fk_agent_runs_approval", ondelete="SET NULL"), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    cancel_requested_by_employee_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentTaskRecord(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','cancel_requested','awaiting_approval','completed','cancelled','failed','timed_out','interrupted')", name="ck_agent_tasks_status"),
        CheckConstraint("event_sequence >= 0", name="ck_agent_tasks_event_sequence"),
        Index("ix_agent_tasks_queue", "status", "created_at"),
        Index("ix_agent_tasks_queue_name", "queue_name", "status", "created_at"),
        Index("ix_agent_tasks_employee_created", "employee_id", "created_at"),
        Index("uq_agent_tasks_active_thread", "thread_id", unique=True,
              postgresql_where=text("status IN ('queued','running','cancel_requested')")),
    )

    task_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    thread_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.thread_id", name="fk_agent_tasks_conversation", ondelete="SET NULL"), nullable=True,
    )
    employee_id: Mapped[UUID] = mapped_column(
        ForeignKey("employees.employee_id", name="fk_agent_tasks_owner", ondelete="RESTRICT"), nullable=False,
    )
    # 首次执行预分配为 task_id；恢复执行时更新为新 run_id，不建立循环外键。
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    message: Mapped[str] = mapped_column(String(2000), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(64), nullable=False, server_default="chexu")
    skill_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation: Mapped[str] = mapped_column(String(24), nullable=False, server_default="chat")
    command_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    worker_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(200), nullable=True)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentTaskEventRecord(Base):
    __tablename__ = "agent_task_events"

    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_tasks.task_id", name="fk_agent_task_events_task", ondelete="CASCADE"), primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PlanCommandRecord(Base):
    __tablename__ = "plan_commands"
    __table_args__ = (
        CheckConstraint("action IN ('continue','revise','replan')", name="ck_plan_commands_action"),
        CheckConstraint("status IN ('queued','applied','rejected','failed')", name="ck_plan_commands_status"),
        Index("ix_plan_commands_queue", "queue_name", "status", "created_at"),
        Index("ix_plan_commands_thread", "thread_id", "status"),
    )

    command_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    thread_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.thread_id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[UUID] = mapped_column(ForeignKey("employees.employee_id", ondelete="RESTRICT"), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_tasks.task_id", ondelete="SET NULL"), nullable=True)
    queue_name: Mapped[str] = mapped_column(String(64), nullable=False, server_default="chexu")
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
