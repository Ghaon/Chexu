from datetime import datetime, timedelta, timezone
import math
from uuid import UUID, uuid4

from openai import APIConnectionError, APITimeoutError
from sqlalchemy import func, select, text

from src.agent.budget import ExecutionBudgetExceeded
from src.agent.config import get_settings
from src.agent.responses import chat_result
from src.database.connection import SessionFactory
from src.database.models import AgentRunRecord, AgentTaskEventRecord, AgentTaskRecord, EmployeeRecord, PlanCommandRecord
from src.domain.execution import ExecutionCancelled, ExecutionTimedOut
from src.domain.tasks import (ACTIVE_TASK_STATUSES, TaskConflict, TaskEvent, TaskEventPage, TaskNotFound,
                              TaskView, TaskWorkItem)


EVENT_FIELDS = frozenset({
    "tool_name", "duration_ms", "error_type", "status", "approval_id", "skill_id", "skill_version",
    "input_tokens", "output_tokens", "total_tokens", "tool_call_count", "result_count", "chunk_count",
    "resource", "limit", "used", "requested", "provider", "span_id", "parent_span_id", "error_code",
    "result_available",
    "step_id", "step_index", "step_count",
    "agent_id", "parent_agent_id", "command_id", "revision",
    "original_estimated_tokens", "input_estimated_tokens", "input_limit", "output_reserved_tokens",
    "retained_messages", "covered_messages", "summary_revision", "estimator",
})


def safe_event_data(data: dict) -> dict:
    result = {}
    for key in EVENT_FIELDS & data.keys():
        value = data[key]
        if value is None or isinstance(value, (bool, int)):
            result[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:200]
    return result


def _append_event(session, record: AgentTaskRecord, event: str, data: dict, run_id: UUID | None = None):
    # 调用方持有任务行锁；序号与事件在同一事务提交，支持断线后按序重放。
    record.event_sequence += 1
    session.add(AgentTaskEventRecord(task_id=record.task_id, sequence=record.event_sequence,
                                     run_id=run_id, event=event[:64], data=safe_event_data(data)))


def _owned(session, task_id: UUID, employee_id: UUID, *, lock=False) -> AgentTaskRecord:
    statement = select(AgentTaskRecord).where(AgentTaskRecord.task_id == task_id,
                                             AgentTaskRecord.employee_id == employee_id)
    if lock:
        statement = statement.with_for_update()
    record = session.scalar(statement)
    if record is None:
        raise TaskNotFound("未找到该后台任务。")
    return record


def get_task(task_id: UUID, employee_id: UUID) -> TaskView:
    with SessionFactory() as session:
        return TaskView.model_validate(_owned(session, task_id, employee_id))


def get_result(task_id: UUID, employee_id: UUID) -> tuple[TaskView, dict | None]:
    with SessionFactory() as session:
        record = _owned(session, task_id, employee_id)
        return TaskView.model_validate(record), record.result


def list_tasks(employee_id: UUID, thread_id: UUID | None, limit: int) -> list[TaskView]:
    with SessionFactory() as session:
        statement = select(AgentTaskRecord).where(AgentTaskRecord.employee_id == employee_id)
        if thread_id is not None:
            statement = statement.where(AgentTaskRecord.thread_id == thread_id)
        records = session.scalars(statement.order_by(AgentTaskRecord.created_at.desc(), AgentTaskRecord.task_id).limit(limit)).all()
        return [TaskView.model_validate(record) for record in records]


def ensure_thread_available(thread_id: UUID, task_id: UUID | None = None) -> None:
    with SessionFactory() as session:
        active = session.scalar(select(AgentTaskRecord.task_id).where(
            AgentTaskRecord.thread_id == thread_id, AgentTaskRecord.status.in_(ACTIVE_TASK_STATUSES)))
        if active is not None and active != task_id:
            raise TaskConflict("该会话已有排队或运行中的后台任务，请等待完成或先取消。")


def submit(message: str, thread_id: UUID | None, skill_id: str | None, employee_id: UUID, role: str) -> TaskView:
    from src.agent.skills import get_skill_registry
    from src.agent.vehicle_agent import build_vehicle_agent
    from src.database.checkpoints import open_checkpointer
    from src.services.conversations import ensure_conversation

    settings = get_settings()
    thread_id, task_id = thread_id or uuid4(), uuid4()
    registry = get_skill_registry()
    if skill_id is not None:
        registry.get_metadata(skill_id, role)
    with open_checkpointer(str(thread_id)) as saver:
        state = build_vehicle_agent(saver).get_state({"configurable": {"thread_id": str(thread_id)}})
        ensure_conversation(str(thread_id), employee_id, message, has_checkpoint=bool(state.values))
        ensure_thread_available(thread_id)
        if state.next:
            raise TaskConflict("该会话有未完成的执行，请先恢复执行或处理审批。")
        with SessionFactory.begin() as session:
            employee = session.get(EmployeeRecord, employee_id, with_for_update=True)
            if employee is None or not employee.is_active:
                raise TaskConflict("当前员工已停用，无法提交任务。")
            pending = session.scalar(select(func.count()).select_from(AgentTaskRecord).where(
                AgentTaskRecord.employee_id == employee_id, AgentTaskRecord.status.in_(ACTIVE_TASK_STATUSES)))
            if pending >= settings.task_max_pending_per_employee:
                raise TaskConflict("当前排队和运行任务已达上限，请等待完成或取消部分任务。")
            record = AgentTaskRecord(task_id=task_id, thread_id=thread_id, employee_id=employee_id, run_id=task_id,
                                      message=message, skill_id=skill_id, status="queued", event_sequence=0,
                                      cancel_requested=False, queue_name=settings.task_queue_name)
            session.add(record)
            session.flush()
            _append_event(session, record, "task.queued", {"status": "queued", "skill_id": skill_id}, task_id)
            session.flush()
            return TaskView.model_validate(record)


def claim_next() -> TaskWorkItem | None:
    with SessionFactory.begin() as session:
        record = session.scalar(select(AgentTaskRecord).where(AgentTaskRecord.status == "queued",
                                AgentTaskRecord.queue_name == get_settings().task_queue_name)
                                .order_by(AgentTaskRecord.created_at, AgentTaskRecord.task_id)
                                .limit(1).with_for_update(skip_locked=True))
        if record is None:
            return None
        if record.thread_id is None:
            record.status, record.error_code = "failed", "conversation_deleted"
            record.error_message = "会话已不可用。"
            record.finished_at = datetime.now(timezone.utc)
            _append_event(session, record, "task.failed", {"status": "failed", "error_code": record.error_code}, record.run_id)
            return None
        record.status = "running"
        record.worker_token = uuid4()
        record.started_at = datetime.now(timezone.utc)
        record.lease_until = record.started_at + timedelta(seconds=get_settings().max_run_seconds + 30)
        _append_event(session, record, "task.claimed", {"status": "running"}, record.run_id)
        return TaskWorkItem(record.task_id, record.thread_id, record.employee_id, record.message,
                            record.skill_id, record.worker_token, record.run_id, record.operation, record.command_id)


def begin_execution(task_id: UUID, run_id: UUID, employee_id: UUID, thread_id: UUID, skill_id: str,
                     worker_token: UUID | None = None, *, lease_seconds: float | None = None) -> None:
    with SessionFactory.begin() as session:
        record = _owned(session, task_id, employee_id, lock=True)
        if worker_token is not None and record.worker_token != worker_token:
            raise TaskConflict("后台任务执行权已变化，停止旧执行。")
        if record.thread_id != thread_id:
            raise TaskConflict("任务与会话不一致，无法执行。")
        record.run_id, record.skill_id = run_id, skill_id
        record.status = "cancel_requested" if record.cancel_requested else "running"
        record.started_at = record.started_at or datetime.now(timezone.utc)
        record.finished_at, record.result, record.error_code, record.error_message = None, None, None, None
        record.worker_token = record.worker_token or uuid4()
        record.lease_until = datetime.now(timezone.utc) + timedelta(seconds=(lease_seconds or get_settings().max_run_seconds) + 30)
        _append_event(session, record, "task.running", {"status": record.status, "skill_id": skill_id}, run_id)


def record_event(task_id: UUID, run_id: UUID, event: str, data: dict) -> None:
    with SessionFactory.begin() as session:
        record = session.get(AgentTaskRecord, task_id, with_for_update=True)
        if record is None or record.run_id != run_id:
            return
        _append_event(session, record, event, data, run_id)


def error_details(error: Exception) -> tuple[str, str, str]:
    if isinstance(error, ExecutionTimedOut):
        return "timed_out", "execution_timed_out", "执行已超时，可查看运行记录后恢复。"
    if isinstance(error, APITimeoutError):
        return "timed_out", "model_request_timed_out", "模型服务响应超时，请稍后恢复执行。"
    if isinstance(error, APIConnectionError):
        return "failed", "model_connection_failed", "无法连接模型服务，请检查模型地址和网络；环境代理异常时可设置 CHEXU_MODEL_USE_ENV_PROXY=false 并重启后端，再恢复执行。"
    if isinstance(error, ExecutionBudgetExceeded):
        return "failed", "execution_budget_exceeded", "本轮调用额度已用完，请取消本轮或缩小任务范围。"
    return "failed", "execution_failed", "执行失败，请查看运行记录后恢复或取消；详情见服务端日志。"


def record_outcome(task_id: UUID, run_id: UUID, *, result: dict | None = None, error: Exception | None = None,
                    worker_token: UUID | None = None) -> None:
    with SessionFactory.begin() as session:
        record = session.get(AgentTaskRecord, task_id, with_for_update=True)
        if record is None or record.run_id != run_id:
            return
        if worker_token is not None and (record.worker_token != worker_token or record.status not in ACTIVE_TASK_STATUSES):
            return
        if isinstance(error, ExecutionCancelled):
            status, code, message = "cancelled", None, None
        elif error is not None:
            status, code, message = error_details(error)
        else:
            status, code, message = result["status"], None, None
        record.status, record.error_code, record.error_message = status, code, message
        if status == "cancelled":
            record.cancel_requested = True
        record.result = chat_result(result, str(run_id), str(record.thread_id)) if result is not None else None
        record.finished_at = datetime.now(timezone.utc)
        record.worker_token, record.lease_until = None, None
        approval = result.get("approval") if result is not None else None
        _append_event(session, record, f"task.{status}", {"status": status, "error_code": code,
                      "result_available": record.result is not None,
                      "approval_id": approval.get("approval_id") if approval else None}, run_id)


def request_cancel(task_id: UUID, employee_id: UUID) -> TaskView:
    with SessionFactory.begin() as session:
        record = _owned(session, task_id, employee_id, lock=True)
        if record.status == "cancelled":
            return TaskView.model_validate(record)
        if record.status not in ACTIVE_TASK_STATUSES:
            raise TaskConflict("该任务已结束或等待审批；失败任务请使用运行取消接口，待审任务请使用审批取消接口。")
        record.cancel_requested = True
        if record.status == "queued":
            record.status, record.finished_at = "cancelled", datetime.now(timezone.utc)
            event = "task.cancelled"
            if record.operation == "plan_action" and record.command_id:
                command = session.get(PlanCommandRecord, record.command_id, with_for_update=True)
                if command and command.status == "queued":
                    command.status, command.error_code, command.finished_at = "rejected", "task_cancelled", record.finished_at
        else:
            record.status, event = "cancel_requested", "task.cancel_requested"
            run = session.get(AgentRunRecord, record.run_id, with_for_update=True)
            if run is not None and run.status in {"running", "cancel_requested"}:
                run.cancel_requested, run.status = True, "cancel_requested"
                run.cancel_requested_by_employee_id = employee_id
        _append_event(session, record, event, {"status": record.status}, record.run_id)
        session.flush()
        return TaskView.model_validate(record)


def list_events(task_id: UUID, employee_id: UUID, after: int, limit: int = 100) -> TaskEventPage:
    with SessionFactory() as session:
        record = _owned(session, task_id, employee_id)
        rows = session.scalars(select(AgentTaskEventRecord).where(
            AgentTaskEventRecord.task_id == task_id, AgentTaskEventRecord.sequence > after,
        ).order_by(AgentTaskEventRecord.sequence).limit(limit + 1)).all()
        events = [TaskEvent.model_validate(row) for row in rows[:limit]]
        return TaskEventPage(events=events, next_sequence=events[-1].sequence if events else after,
                              has_more=len(rows) > limit, status=record.status)


def recover_expired() -> int:
    recovered = 0
    with SessionFactory.begin() as session:
        records = session.scalars(select(AgentTaskRecord).where(
            AgentTaskRecord.status.in_(["running", "cancel_requested"]),
            AgentTaskRecord.queue_name == get_settings().task_queue_name,
            AgentTaskRecord.lease_until < datetime.now(timezone.utc),
        ).order_by(AgentTaskRecord.created_at).limit(20).with_for_update(skip_locked=True)).all()
        for record in records:
            if record.thread_id is not None:
                acquired = session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                                           {"key": f"chexu-chat:{record.thread_id}"})
                if not acquired:
                    continue
            run = session.get(AgentRunRecord, record.run_id, with_for_update=True)
            if run is not None and record.thread_id:
                from src.agent.runtime import pending_approval_id, present_result
                from src.agent.tracing import AgentTrace
                from src.agent.vehicle_agent import build_vehicle_agent
                from src.database.checkpoints import open_checkpointer

                # 会话锁已由当前事务持有；只读取检查点，不重放任何模型或工具。
                with open_checkpointer() as saver:
                    state = build_vehicle_agent(saver).get_state({"configurable": {"thread_id": str(record.thread_id)}}, subgraphs=True)
                result = None
                if state.values.get("turn_id") == str(run.turn_id):
                    if pending_approval_id(state):
                        pauses = [item for task in state.tasks for item in task.interrupts]
                        result = present_result({"__interrupt__": pauses, "plan": state.values.get("plan")},
                                                AgentTrace(str(run.run_id), str(record.thread_id)))
                    elif not state.next and state.values.get("reply"):
                        status = state.values.get("execution_status") or (run.status if run.status in {"completed", "cancelled"} else "completed")
                        result = state.values | {"status": status}
                if result is not None:
                    if result.get("approval"):
                        from src.services import approvals

                        approval = approvals.get_task(UUID(result["approval"]["approval_id"]))
                        if not state.next and not approval.workflow_completed:
                            approval = approvals.finish_resume(approval.approval_id, completed=True)
                        result = result | {"approval": approval.model_dump(mode="json")}
                    result = result | {"skill_id": run.skill_id, "skill_version": run.skill_version}
                    record.status, record.error_code, record.error_message = result["status"], None, None
                    record.result = chat_result(result, str(run.run_id), str(record.thread_id))
                    record.finished_at = datetime.now(timezone.utc)
                    run.status, run.finished_at = result["status"], record.finished_at
                    if result.get("approval"):
                        run.approval_id = UUID(result["approval"]["approval_id"])
                    record.worker_token, record.lease_until = None, None
                    _append_event(session, record, f"task.{record.status}", {"status": record.status, "result_available": True}, run.run_id)
                    recovered += 1
                    continue
            record.status, record.error_code = "interrupted", "worker_interrupted"
            record.error_message = "后台执行已中断，请通过运行记录恢复；不会自动重放业务操作。"
            record.finished_at = datetime.now(timezone.utc)
            record.worker_token, record.lease_until = None, None
            if run is not None and run.status in {"running", "cancel_requested"}:
                run.status, run.error_type, run.finished_at = "failed", "WorkerInterrupted", record.finished_at
            _append_event(session, record, "task.interrupted", {"status": record.status, "error_code": record.error_code}, record.run_id)
            recovered += 1
    return recovered
