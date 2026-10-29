"""Add explicit quote confirmation state."""

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("ck_quotes_draft_status", "quotes", type_="check")
    op.drop_constraint("ck_quotes_requires_confirmation", "quotes", type_="check")
    op.create_check_constraint("ck_quotes_status", "quotes", "status IN ('draft', 'confirmed')")
    op.create_check_constraint("ck_quotes_not_total_price", "quotes", "NOT is_total_price")
    op.create_check_constraint(
        "ck_quotes_confirmation_state", "quotes",
        "(status = 'draft' AND requires_confirmation AND confirmed_at IS NULL) OR "
        "(status = 'confirmed' AND NOT requires_confirmation AND confirmed_at IS NOT NULL)",
    )


def downgrade() -> None:
    # 已确认的报价不能在降级时被悄悄改回草稿。
    confirmed_exists = op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM quotes WHERE status = 'confirmed')"))
    if confirmed_exists:
        raise RuntimeError("存在已确认报价，不能降级到只支持草稿的版本。")
    op.drop_constraint("ck_quotes_confirmation_state", "quotes", type_="check")
    op.drop_constraint("ck_quotes_not_total_price", "quotes", type_="check")
    op.drop_constraint("ck_quotes_status", "quotes", type_="check")
    op.create_check_constraint("ck_quotes_draft_status", "quotes", "status = 'draft'")
    op.create_check_constraint("ck_quotes_requires_confirmation", "quotes", "NOT is_total_price AND requires_confirmation")
    op.drop_column("quotes", "confirmed_at")
