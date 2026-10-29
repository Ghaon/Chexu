"""Fence plan commands to the original conversation turn."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("plan_commands", sa.Column("turn_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE plan_commands SET status='rejected', error_code='turn_unknown', finished_at=now() WHERE status='queued'")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM plan_commands WHERE status='queued')")):
        raise RuntimeError("存在待处理调整指令，不能移除任务轮次校验。")
    op.drop_column("plan_commands", "turn_id")
