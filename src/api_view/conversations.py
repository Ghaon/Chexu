import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from psycopg import Error as PostgresError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.agent.vehicle_agent import build_vehicle_agent
from src.agent.runtime import pending_approval_id
from src.api_view.auth import get_current_employee
from src.api_view.schemas import (ConversationCreate, ConversationHistory, ConversationList,
                                  ConversationMessage, ConversationResponse, EmployeeResponse)
from src.database.checkpoints import ConversationBusyError, open_checkpointer
from src.database.connection import SessionFactory
from src.database.models import ApprovalRecord, ConversationRecord
from src.services.conversations import ConversationAccessError, owned_conversation
from src.services import tasks
from src.domain.tasks import TaskConflict
from src.domain.planning import PlanView, public_plan
from src.domain.conversation_context import ConversationContextView
from src.services import approvals, executions


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["conversations"])


def conversation_error(error: Exception) -> HTTPException:
    if isinstance(error, ConversationAccessError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ConversationBusyError):
        return HTTPException(status_code=409, detail=str(error))
    logger.warning("Conversation storage failed (%s).", type(error).__name__)
    return HTTPException(status_code=503, detail="会话存储暂时不可用，请稍后重试。")


@router.post("", response_model=ConversationResponse, status_code=201)
def create_conversation(body: ConversationCreate,
                        employee: EmployeeResponse = Depends(get_current_employee)) -> ConversationResponse:
    try:
        with SessionFactory.begin() as session:
            record = ConversationRecord(employee_id=employee.employee_id, title=body.title)
            session.add(record)
            session.flush()
            response = ConversationResponse.model_validate(record)
        return response
    except SQLAlchemyError as exc:
        raise conversation_error(exc) from exc


@router.get("", response_model=ConversationList)
def list_conversations(limit: int = Query(default=20, ge=1, le=100),
                       offset: int = Query(default=0, ge=0, le=100000),
                       employee: EmployeeResponse = Depends(get_current_employee)) -> ConversationList:
    try:
        with SessionFactory() as session:
            records = session.scalars(select(ConversationRecord).where(
                ConversationRecord.employee_id == employee.employee_id
            ).order_by(ConversationRecord.updated_at.desc(), ConversationRecord.thread_id)
                .offset(offset).limit(limit + 1)).all()
            return ConversationList(conversations=[ConversationResponse.model_validate(r) for r in records[:limit]],
                                    has_more=len(records) > limit)
    except SQLAlchemyError as exc:
        raise conversation_error(exc) from exc


@router.get("/{thread_id}", response_model=ConversationHistory)
def conversation_history(thread_id: UUID,
                         employee: EmployeeResponse = Depends(get_current_employee)) -> ConversationHistory:
    try:
        with open_checkpointer(str(thread_id)) as saver:
            with SessionFactory() as session:
                info = ConversationResponse.model_validate(owned_conversation(session, thread_id, employee.employee_id))
            state = build_vehicle_agent(saver).get_state({"configurable": {"thread_id": str(thread_id)}}, subgraphs=True)
            messages = []
            for message in state.values.get("messages", []):
                if isinstance(message, HumanMessage):
                    messages.append(ConversationMessage(role="user", content=message.content))
                elif isinstance(message, AIMessage):
                    messages.append(ConversationMessage(role="assistant", content=message.content,
                                                        tool_calls=message.tool_calls))
                elif isinstance(message, ToolMessage):
                    messages.append(ConversationMessage(role="tool", content=message.content,
                                                        tool_name=message.name, tool_call_id=message.tool_call_id))
            return ConversationHistory(**info.model_dump(), messages=messages, pending_approval_id=pending_approval_id(state))
    except (ConversationAccessError, ConversationBusyError, SQLAlchemyError, PostgresError) as exc:
        raise conversation_error(exc) from exc


@router.get("/{thread_id}/context", response_model=ConversationContextView)
def conversation_context(thread_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        with SessionFactory() as session:
            owned_conversation(session, thread_id, employee.employee_id)
        with open_checkpointer() as saver:
            state = build_vehicle_agent(saver).get_state({"configurable": {"thread_id": str(thread_id)}})
        return ConversationContextView(total_messages=len(state.values.get("messages", [])),
            digest=state.values.get("context_digest"), usage=state.values.get("context_usage"))
    except (ConversationAccessError, SQLAlchemyError, PostgresError) as exc:
        raise conversation_error(exc) from exc


@router.get("/{thread_id}/plan", response_model=PlanView | None)
def conversation_plan(thread_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        with SessionFactory() as session:
            owned_conversation(session, thread_id, employee.employee_id)
        # 观察进度不获取执行锁；只读取最近已提交的检查点。
        with open_checkpointer() as saver:
            state = build_vehicle_agent(saver).get_state({"configurable": {"thread_id": str(thread_id)}})
        plan = state.values.get("plan")
        if not plan:
            return None
        plan = {**plan, "turn_id": state.values.get("turn_id")}
        latest = executions.latest(UUID(state.values["turn_id"]))
        status = None
        if latest and latest["status"] in {"failed", "timed_out", "cancelled", "awaiting_approval"}:
            status = latest["status"]
        approval = state.values.get("approval")
        if approval:
            approval = approvals.get_task(UUID(approval["approval_id"])).model_dump(mode="json")
        return public_plan(plan, status=status, approval=approval)
    except (ConversationAccessError, SQLAlchemyError, PostgresError) as exc:
        raise conversation_error(exc) from exc


@router.delete("/{thread_id}", status_code=204)
def delete_conversation(thread_id: UUID,
                        employee: EmployeeResponse = Depends(get_current_employee)) -> Response:
    try:
        # 与聊天共用执行锁，防止边执行边删除；先清检查点，失败时保留归属供重试。
        with open_checkpointer(str(thread_id)) as saver, SessionFactory.begin() as session:
            record = owned_conversation(session, thread_id, employee.employee_id)
            try:
                tasks.ensure_thread_available(thread_id)
            except TaskConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            unfinished = session.scalar(select(ApprovalRecord.approval_id).where(
                ApprovalRecord.thread_id == thread_id, ApprovalRecord.status.in_(["pending", "approved"]),
            ).limit(1))
            if unfinished is not None:
                raise HTTPException(status_code=409, detail="会话有未完成的审批，请先处理或取消审批任务。")
            saver.delete_thread(str(thread_id))
            session.delete(record)
        return Response(status_code=204)
    except (ConversationAccessError, ConversationBusyError, SQLAlchemyError, PostgresError) as exc:
        raise conversation_error(exc) from exc
