"""Owner-scoped confirmed preferences and unconfirmed candidates."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("user_memories",
        sa.Column("memory_id", sa.Uuid(), primary_key=True),
        sa.Column("employee_id", sa.Uuid(), sa.ForeignKey("employees.employee_id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("source_quote", sa.String(500)),
        sa.Column("source_thread_id", sa.Uuid(), sa.ForeignKey("conversations.thread_id", ondelete="SET NULL")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("category IN ('budget', 'usage', 'preferences')", name="ck_user_memories_category"),
        sa.CheckConstraint("status IN ('pending', 'confirmed')", name="ck_user_memories_status"),
        sa.CheckConstraint("source IN ('user_entered', 'agent_proposed')", name="ck_user_memories_source"),
        sa.CheckConstraint("revision > 0", name="ck_user_memories_revision"),
        sa.CheckConstraint("(status = 'confirmed') = (confirmed_at IS NOT NULL)", name="ck_user_memories_confirmation"))
    op.create_index("ix_user_memories_owner_status", "user_memories", ["employee_id", "status"])
    op.create_index("uq_user_memories_confirmed_category", "user_memories", ["employee_id", "category"],
                    unique=True, postgresql_where=sa.text("status = 'confirmed'"))


def downgrade():
    op.drop_table("user_memories")
