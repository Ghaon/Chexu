from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from psycopg import Error as PostgresError

from src.api_view.auth import get_current_employee
from src.api_view.schemas import EmployeeResponse
from src.domain.planning import PlanActionInput, PlanActionView
from src.services import plan_actions
from src.services.conversations import ConversationAccessError


router = APIRouter(prefix="/api/conversations/{thread_id}/plan/actions", tags=["agent-plans"])


def action_error(error):
    if isinstance(error, ConversationAccessError):
        return HTTPException(404, str(error))
    if isinstance(error, plan_actions.PlanActionConflict):
        return HTTPException(409, str(error))
    if isinstance(error, IntegrityError):
        return HTTPException(409, "调整指令已被提交，请使用原 command_id 查询结果。")
    return HTTPException(503, "计划调整服务暂时不可用。")


@router.post("", response_model=PlanActionView, status_code=202)
def submit_action(thread_id: UUID, body: PlanActionInput, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return plan_actions.submit(thread_id, employee.employee_id, body)
    except (ConversationAccessError, plan_actions.PlanActionConflict, SQLAlchemyError, PostgresError) as exc:
        raise action_error(exc) from exc


@router.get("/{command_id}", response_model=PlanActionView)
def get_action(thread_id: UUID, command_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return plan_actions.get_action(thread_id, command_id, employee.employee_id)
    except (ConversationAccessError, SQLAlchemyError) as exc:
        raise action_error(exc) from exc
