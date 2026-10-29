"""Add conversation ownership, quote authors and login throttling."""

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("created_by_employee_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_quotes_creator", "quotes", "employees",
                          ["created_by_employee_id"], ["employee_id"], ondelete="RESTRICT")
    op.create_table(
        "conversations",
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("thread_id"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.employee_id"],
                                name="fk_conversations_employee", ondelete="CASCADE"),
    )
    op.create_index("ix_conversations_employee_updated", "conversations", ["employee_id", "updated_at"])
    op.create_table(
        "login_attempts",
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("username"),
        sa.CheckConstraint("failed_attempts >= 0", name="ck_login_attempts_nonnegative"),
    )


def downgrade() -> None:
    has_owners = op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM conversations) OR "
        "EXISTS (SELECT 1 FROM quotes WHERE created_by_employee_id IS NOT NULL)"
    ))
    if has_owners:
        raise RuntimeError("已有会话归属或报价创建人记录，不能在降级时删除这些信息。")
    op.drop_table("login_attempts")
    op.drop_table("conversations")
    op.drop_constraint("fk_quotes_creator", "quotes", type_="foreignkey")
    op.drop_column("quotes", "created_by_employee_id")
