"""Persist execution controls and skill snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("initiated_by_employee_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.String(64), nullable=False),
        sa.Column("skill_version", sa.String(32), nullable=False),
        sa.Column("context_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(24), server_default="running", nullable=False),
        sa.Column("model_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("cancel_requested_by_employee_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(["parent_run_id"], ["agent_runs.run_id"], name="fk_agent_runs_parent", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["thread_id"], ["conversations.thread_id"], name="fk_agent_runs_conversation", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.employee_id"], name="fk_agent_runs_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["initiated_by_employee_id"], ["employees.employee_id"], name="fk_agent_runs_actor", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.approval_id"], name="fk_agent_runs_approval", ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('running','cancel_requested','completed','awaiting_approval','failed','cancelled','timed_out')", name="ck_agent_runs_status"),
        sa.CheckConstraint("model_calls >= 0 AND tool_calls >= 0", name="ck_agent_runs_counts"),
    )
    op.create_index("ix_agent_runs_turn", "agent_runs", ["turn_id"])
    op.create_index("ix_agent_runs_employee_started", "agent_runs", ["employee_id", "started_at"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_runs)")):
        raise RuntimeError("已有执行记录，不能在降级时删除执行历史。")
    op.drop_table("agent_runs")
