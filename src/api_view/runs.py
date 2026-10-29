import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from langgraph.errors import GraphRecursionError
from openai import APIConnectionError, APIError, APITimeoutError
from psycopg import Error as PostgresError
from sqlalchemy.exc import SQLAlchemyError

from src.agent.budget import ExecutionBudgetExceeded
from src.agent.runtime import cancel_run, resume_run
from src.agent.skills import SkillError
from src.api_view.auth import get_current_employee
from src.api_view.schemas import ChatResponse, EmployeeResponse
from src.database.checkpoints import ConversationBusyError
from src.domain.execution import ExecutionTimedOut, RunConflict, RunNotFound, RunView
from src.domain.conversation_context import ContextCapacityExceeded
from src.services import executions, tasks
from src.services.conversations import ConversationAccessError


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/runs", tags=["runs"])


def run_error(error: Exception, run_id: str | None = None) -> HTTPException:
    status = 500
    detail = {"message": "执行服务处理失败，请检查服务端日志。", "run_id": run_id}
    if isinstance(error, (RunNotFound, ConversationAccessError)):
        status, detail["message"] = 404, str(error)
    elif isinstance(error, (RunConflict, ConversationBusyError, SkillError)):
        status, detail["message"] = 409, str(error)
    elif isinstance(error, ContextCapacityExceeded):
        status = 422
        detail.update(message=str(error), code="context_capacity_exceeded")
    elif isinstance(error, ExecutionTimedOut):
        status, detail["message"] = 504, str(error)
    elif isinstance(error, ExecutionBudgetExceeded):
        status = 422
        detail.update(message=str(error), code="execution_budget_exceeded", resource=error.resource,
                      limit=error.limit, used=error.used, requested=error.requested)
    elif isinstance(error, (SQLAlchemyError, PostgresError)):
        status, detail["message"] = 503, "执行记录存储暂时不可用，请稍后重试。"
    elif isinstance(error, APIConnectionError):
        status = 504 if isinstance(error, APITimeoutError) else 502
        _, code, message = tasks.error_details(error)
        detail.update(message=message, code=code)
    elif isinstance(error, (APIError, GraphRecursionError)):
        status, detail["message"] = 502, "模型执行暂时失败，可查看运行记录后恢复。"
    if status >= 500:
        logger.warning("Run operation failed (%s).", type(error).__name__)
    return HTTPException(status_code=status, detail=detail)


@router.get("", response_model=list[RunView])
def list_runs(thread_id: UUID | None = None, limit: int = Query(default=20, ge=1, le=100),
              employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return executions.list_runs(employee.employee_id, thread_id, limit)
    except Exception as exc:
        raise run_error(exc) from exc


@router.get("/{run_id}", response_model=RunView)
def get_run(run_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return executions.get_run(run_id, employee.employee_id)
    except Exception as exc:
        raise run_error(exc, str(run_id)) from exc


@router.post("/{run_id}/cancel", response_model=RunView, status_code=202)
def cancel(run_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return cancel_run(run_id, employee.employee_id)
    except Exception as exc:
        raise run_error(exc, str(run_id)) from exc


@router.post("/{run_id}/resume", response_model=ChatResponse)
def resume(run_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    new_id = str(uuid4())
    try:
        return resume_run(run_id, employee.employee_id, employee.role, new_id)
    except Exception as exc:
        raise run_error(exc, new_id) from exc
