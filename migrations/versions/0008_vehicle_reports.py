"""Associate immutable uploaded reports with inventory vehicles."""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vehicle_reports",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_record_id", sa.Integer(), nullable=False),
        sa.Column("vehicle_id", sa.String(32), nullable=False),
        sa.Column("uploaded_by_employee_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("suffix", sa.String(8), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("report_id"),
        sa.ForeignKeyConstraint(["vehicle_record_id"], ["vehicles.id"], name="fk_vehicle_reports_vehicle", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by_employee_id"], ["employees.employee_id"], name="fk_vehicle_reports_uploader", ondelete="RESTRICT"),
        sa.UniqueConstraint("vehicle_record_id", "sha256", name="uq_vehicle_reports_content"),
        sa.CheckConstraint("suffix IN ('.pdf','.docx','.txt','.md')", name="ck_vehicle_reports_suffix"),
        sa.CheckConstraint("size_bytes > 0", name="ck_vehicle_reports_size"),
    )
    op.create_index("ix_vehicle_reports_vehicle_created", "vehicle_reports", ["vehicle_record_id", "created_at"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM vehicle_reports)")):
        raise RuntimeError("已有车辆报告，不能在降级时删除资料关联。")
    op.drop_table("vehicle_reports")
