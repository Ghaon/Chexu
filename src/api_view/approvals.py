import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import Error as PostgresError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.agent.runtime import resume_approval
from src.agent.skills import SkillError
from src.domain.execution import RunConflict
from src.api_view.auth import get_current_employee, require_quote_confirmer
from src.api_view.schemas import ApprovalList, ApprovalResolution, ApprovalReview, EmployeeResponse
from src.database.checkpoints import ConversationBusyError
from src.database.connection import SessionFactory
from src.database.models import ApprovalRecord
from src.domain.approvals import ApprovalStatus, ApprovalTask
from src.services import approvals


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def approval_error(error: Exception) -> HTTPException:
    if isinstance(error, approvals.ApprovalNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (approvals.ApprovalConflict, ConversationBusyError, SkillError, RunConflict)):
        return HTTPException(status_code=409, detail=str(error))
    logger.warning("Approval storage failed (%s).", type(error).__name__)
    return HTTPException(status_code=503, detail="审批服务暂时不可用，请稍后重试。")


@router.get("", response_model=ApprovalList)
def list_approvals(status: ApprovalStatus | None = None, limit: int = Query(default=20, ge=1, le=100),
                   offset: int = Query(default=0, ge=0, le=100000),
                   employee: EmployeeResponse = Depends(get_current_employee)) -> ApprovalList:
    try:
        statement = select(ApprovalRecord)
        if employee.role != "manager":
            statement = statement.where(ApprovalRecord.requested_by_employee_id == employee.employee_id)
        if status is not None:
            statement = statement.where(ApprovalRecord.status == status)
        with SessionFactory() as session:
            records = session.scalars(statement.order_by(ApprovalRecord.created_at.desc(), ApprovalRecord.approval_id)
                                      .offset(offset).limit(limit + 1)).all()
            return ApprovalList(approvals=[ApprovalTask.model_validate(r) for r in records[:limit]],
                                 has_more=len(records) > limit)
    except SQLAlchemyError as exc:
        raise approval_error(exc) from exc


@router.get("/{approval_id}", response_model=ApprovalTask)
def get_approval(approval_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)) -> ApprovalTask:
    try:
        return approvals.get_task(approval_id, employee.employee_id, employee.role == "manager")
    except (approvals.ApprovalNotFound, SQLAlchemyError) as exc:
        raise approval_error(exc) from exc


def resolve(approval_id: UUID, employee: EmployeeResponse, decision: str, note: str) -> ApprovalResolution:
    run_id = str(uuid4())
    try:
        task, status = resume_approval(approval_id, employee.employee_id, decision, note, run_id,
                                       manager=employee.role == "manager")
        return ApprovalResolution(run_id=run_id, approval=task, execution_status=status)
    except (approvals.ApprovalNotFound, approvals.ApprovalConflict, ConversationBusyError,
            SQLAlchemyError, PostgresError, SkillError, RunConflict) as exc:
        raise approval_error(exc) from exc


@router.post("/{approval_id}/review", response_model=ApprovalResolution)
def review_approval(approval_id: UUID, body: ApprovalReview,
                     employee: EmployeeResponse = Depends(require_quote_confirmer)) -> ApprovalResolution:
    return resolve(approval_id, employee, body.decision, body.note)


@router.post("/{approval_id}/cancel", response_model=ApprovalResolution)
def cancel_approval(approval_id: UUID,
                     employee: EmployeeResponse = Depends(get_current_employee)) -> ApprovalResolution:
    return resolve(approval_id, employee, "cancel", "申请已取消。")
