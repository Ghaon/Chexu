"""Persist quote approval tasks for interrupted graphs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("requested_by_employee_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_username", sa.String(32), nullable=False),
        sa.Column("vehicle_record_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("quote_draft", postgresql.JSONB(), nullable=False),
        sa.Column("decision", sa.String(8), nullable=True),
        sa.Column("decided_by_employee_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(500), server_default="", nullable=False),
        sa.Column("quote_id", sa.Uuid(), nullable=True),
        sa.Column("failure_reason", sa.String(200), nullable=True),
        sa.Column("resume_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_resume_error", sa.String(64), nullable=True),
        sa.Column("workflow_completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint("thread_id", "request_key", name="uq_approvals_request"),
        sa.UniqueConstraint("quote_id", name="uq_approvals_quote"),
        sa.ForeignKeyConstraint(["thread_id"], ["conversations.thread_id"], name="fk_approvals_conversation", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["requested_by_employee_id"], ["employees.employee_id"], name="fk_approvals_requester", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decided_by_employee_id"], ["employees.employee_id"], name="fk_approvals_decider", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["vehicle_record_id"], ["vehicles.id"], name="fk_approvals_vehicle", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.quote_id"], name="fk_approvals_quote", ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('pending','approved','rejected','completed','failed','cancelled','expired')", name="ck_approvals_status"),
        sa.CheckConstraint("resume_attempts >= 0", name="ck_approvals_resume_attempts"),
        sa.CheckConstraint("(status = 'completed') = (quote_id IS NOT NULL)", name="ck_approvals_quote_result"),
        sa.CheckConstraint(
            "(status = 'pending' AND decision IS NULL AND decided_by_employee_id IS NULL AND decided_at IS NULL) OR "
            "(status IN ('approved','completed','failed') AND decision IS NOT NULL AND decision = 'approve' AND decided_by_employee_id IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status = 'rejected' AND decision IS NOT NULL AND decision = 'reject' AND decided_by_employee_id IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status = 'cancelled' AND decision IS NOT NULL AND decision = 'cancel' AND decided_by_employee_id IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status = 'expired' AND decision IS NULL AND decided_at IS NOT NULL)", name="ck_approvals_decision",
        ),
    )
    op.create_index("ix_approvals_status_created", "approvals", ["status", "created_at"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM approvals)")):
        raise RuntimeError("已有审批记录，不能在降级时删除审批历史。")
    op.drop_table("approvals")
