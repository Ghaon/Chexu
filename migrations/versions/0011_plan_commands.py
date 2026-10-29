"""Persist plan actions and route continuation jobs through the task queue."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_tasks", sa.Column("operation", sa.String(24), nullable=False, server_default="chat"))
    op.add_column("agent_tasks", sa.Column("command_id", sa.Uuid(), nullable=True))
    op.create_table("plan_commands",
        sa.Column("command_id", sa.Uuid(), primary_key=True),
        sa.Column("thread_id", sa.Uuid(), sa.ForeignKey("conversations.thread_id", ondelete="CASCADE"), nullable=False),
        sa.Column("employee_id", sa.Uuid(), sa.ForeignKey("employees.employee_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("agent_tasks.task_id", ondelete="SET NULL"), nullable=True),
        sa.Column("queue_name", sa.String(64), nullable=False, server_default="chexu"),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("action IN ('continue','revise','replan')", name="ck_plan_commands_action"),
        sa.CheckConstraint("status IN ('queued','applied','rejected','failed')", name="ck_plan_commands_status"))
    op.create_index("ix_plan_commands_queue", "plan_commands", ["queue_name", "status", "created_at"])
    op.create_index("ix_plan_commands_thread", "plan_commands", ["thread_id", "status"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM plan_commands)")):
        raise RuntimeError("存在计划调整记录，不能直接降级。")
    op.drop_table("plan_commands")
    op.drop_column("agent_tasks", "command_id")
    op.drop_column("agent_tasks", "operation")
