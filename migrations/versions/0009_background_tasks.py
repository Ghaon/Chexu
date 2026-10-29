"""Persist background Agent tasks and replayable events."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.String(2000), nullable=False),
        sa.Column("skill_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), server_default="queued", nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("worker_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(200), nullable=True),
        sa.Column("event_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("task_id"),
        sa.ForeignKeyConstraint(["thread_id"], ["conversations.thread_id"], name="fk_agent_tasks_conversation", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.employee_id"], name="fk_agent_tasks_owner", ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('queued','running','cancel_requested','awaiting_approval','completed','cancelled','failed','timed_out','interrupted')", name="ck_agent_tasks_status"),
        sa.CheckConstraint("event_sequence >= 0", name="ck_agent_tasks_event_sequence"),
    )
    op.create_index("ix_agent_tasks_queue", "agent_tasks", ["status", "created_at"])
    op.create_index("ix_agent_tasks_employee_created", "agent_tasks", ["employee_id", "created_at"])
    op.create_index("uq_agent_tasks_active_thread", "agent_tasks", ["thread_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued','running','cancel_requested')"))
    op.create_table(
        "agent_task_events",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "sequence"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.task_id"], name="fk_agent_task_events_task", ondelete="CASCADE"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_tasks)")):
        raise RuntimeError("已有后台任务，不能在降级时删除任务和事件历史。")
    op.drop_table("agent_task_events")
    op.drop_table("agent_tasks")
