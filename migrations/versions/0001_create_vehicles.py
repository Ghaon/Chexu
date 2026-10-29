"""create_vehicles

Revision ID: 0001
Revises: 
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vehicles",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("vehicle_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("price_cny", sa.Integer(), nullable=False),
        sa.Column("mileage_km", sa.Integer(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_vehicles_title_not_blank"),
        sa.CheckConstraint("length(btrim(vehicle_id)) > 0", name="ck_vehicles_id_not_blank"),
        sa.CheckConstraint("mileage_km >= 0", name="ck_vehicles_mileage_nonnegative"),
        sa.CheckConstraint("price_cny >= 0", name="ck_vehicles_price_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vehicle_id", name="uq_vehicles_vehicle_id"),
    )


def downgrade() -> None:
    op.drop_table("vehicles")
