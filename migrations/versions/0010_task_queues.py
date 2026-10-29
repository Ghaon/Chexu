"""Route workers to named queues without sharing development/test workloads."""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_tasks", sa.Column("queue_name", sa.String(64), server_default="chexu", nullable=False))
    op.create_index("ix_agent_tasks_queue_name", "agent_tasks", ["queue_name", "status", "created_at"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_tasks WHERE queue_name <> 'chexu')")):
        raise RuntimeError("已有其他队列任务，不能在降级时改变任务路由。")
    op.drop_index("ix_agent_tasks_queue_name", table_name="agent_tasks")
    op.drop_column("agent_tasks", "queue_name")
