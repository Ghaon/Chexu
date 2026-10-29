"""Add employee login and quote confirmation identity."""

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "employees",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("employee_id"),
        sa.UniqueConstraint("username", name="uq_employees_username"),
        sa.CheckConstraint("role IN ('sales', 'manager')", name="ck_employees_role"),
        sa.CheckConstraint("username ~ '^[a-z][a-z0-9_.-]{2,31}$'", name="ck_employees_username"),
    )
    op.create_table(
        "employee_tokens",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("token_hash"),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.employee_id"],
                                name="fk_employee_tokens_employee", ondelete="CASCADE"),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_employee_tokens_hash_length"),
    )
    op.create_index("ix_employee_tokens_employee_id", "employee_tokens", ["employee_id"])
    # 允许旧版本已确认报价保留空确认人，不伪造历史操作者。
    op.add_column("quotes", sa.Column("confirmed_by_employee_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_quotes_confirmer", "quotes", "employees",
                          ["confirmed_by_employee_id"], ["employee_id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_quotes_confirmer_state", "quotes",
                               "status = 'confirmed' OR confirmed_by_employee_id IS NULL")


def downgrade() -> None:
    has_attribution = op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM quotes WHERE confirmed_by_employee_id IS NOT NULL)"
    ))
    if has_attribution:
        raise RuntimeError("已有报价记录了确认人，不能在降级时删除确认人信息。")
    op.drop_constraint("ck_quotes_confirmer_state", "quotes", type_="check")
    op.drop_constraint("fk_quotes_confirmer", "quotes", type_="foreignkey")
    op.drop_column("quotes", "confirmed_by_employee_id")
    op.drop_table("employee_tokens")
    op.drop_table("employees")
