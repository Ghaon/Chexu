"""Create saved quote drafts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quotes",
        sa.Column("quote_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_record_id", sa.Integer(), nullable=False),
        sa.Column("vehicle_id", sa.String(32), nullable=False),
        sa.Column("vehicle_title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("currency", sa.String(3), server_default="CNY", nullable=False),
        sa.Column("listed_price_cny", sa.Integer(), nullable=False),
        sa.Column("discount_cny", sa.Integer(), nullable=False),
        sa.Column("quoted_price_cny", sa.Integer(), nullable=False),
        sa.Column("pending_items", postgresql.JSONB(), nullable=False),
        sa.Column("is_total_price", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("requires_confirmation", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("quote_id"),
        sa.ForeignKeyConstraint(["vehicle_record_id"], ["vehicles.id"], name="fk_quotes_vehicle", ondelete="RESTRICT"),
        sa.CheckConstraint("listed_price_cny >= 0", name="ck_quotes_price_nonnegative"),
        sa.CheckConstraint("discount_cny >= 0 AND discount_cny <= listed_price_cny", name="ck_quotes_discount_range"),
        sa.CheckConstraint("quoted_price_cny = listed_price_cny - discount_cny", name="ck_quotes_price_calculation"),
        sa.CheckConstraint("status = 'draft'", name="ck_quotes_draft_status"),
        sa.CheckConstraint("currency = 'CNY'", name="ck_quotes_currency"),
        sa.CheckConstraint("NOT is_total_price AND requires_confirmation", name="ck_quotes_requires_confirmation"),
    )


def downgrade() -> None:
    op.drop_table("quotes")
