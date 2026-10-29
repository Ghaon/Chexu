from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, or_, select

from src.agent.budget import ExecutionBudgetExceeded
from src.agent.config import get_settings
from src.database.connection import SessionFactory
from src.database.models import AgentRunRecord, AgentTaskRecord, EmployeeRecord
from src.domain.execution import ExecutionCancelled, RunConflict, RunNotFound, RunView


def scope(employee_id: UUID):
    return or_(AgentRunRecord.employee_id == employee_id,
               AgentRunRecord.initiated_by_employee_id == employee_id)


def current_role(employee_id: UUID) -> str:
    with SessionFactory() as session:
        employee = session.get(EmployeeRecord, employee_id)
        if employee is None or not employee.is_active:
            raise RunConflict("会话发起员工已停用，无法继续执行。")
        return employee.role


def get_run(run_id: UUID, employee_id: UUID) -> RunView:
    with SessionFactory() as session:
        record = session.scalar(select(AgentRunRecord).where(AgentRunRecord.run_id == run_id, scope(employee_id)))
        if record is None:
            raise RunNotFound("未找到该执行记录。")
        return RunView.model_validate(record)


def list_runs(employee_id: UUID, thread_id: UUID | None, limit: int) -> list[RunView]:
    with SessionFactory() as session:
        statement = select(AgentRunRecord).where(scope(employee_id))
        if thread_id is not None:
            statement = statement.where(AgentRunRecord.thread_id == thread_id)
        return [RunView.model_validate(row) for row in session.scalars(statement.order_by(
            AgentRunRecord.started_at.desc(), AgentRunRecord.run_id).limit(limit)).all()]


def latest(turn_id: UUID) -> dict | None:
    with SessionFactory() as session:
        record = session.scalar(select(AgentRunRecord).where(AgentRunRecord.turn_id == turn_id)
                                .order_by(AgentRunRecord.started_at.desc()).limit(1))
        if record is None:
            return None
        return RunView.model_validate(record).model_dump() | {"context_snapshot": record.context_snapshot}


def usage(turn_id: UUID) -> tuple[int, int, int]:
    with SessionFactory() as session:
        row = session.execute(select(func.coalesce(func.sum(AgentRunRecord.model_calls), 0),
                                     func.coalesce(func.sum(AgentRunRecord.tool_calls), 0), func.count())
                              .where(AgentRunRecord.turn_id == turn_id)).one()
        return tuple(row)


def start_run(*, run_id: UUID, turn_id: UUID, thread_id: UUID, employee_id: UUID,
              actor_id: UUID, snapshot: dict, parent_id: UUID | None = None,
              approval_id: UUID | None = None) -> None:
    now = datetime.now(timezone.utc)
    with SessionFactory.begin() as session:
        task_id = snapshot.get("task_id")
        task = session.get(AgentTaskRecord, UUID(task_id), with_for_update=True) if task_id else None
        cancelled = bool(task and task.cancel_requested)
        session.add(AgentRunRecord(
            run_id=run_id, turn_id=turn_id, parent_run_id=parent_id, thread_id=thread_id,
            employee_id=employee_id, initiated_by_employee_id=actor_id,
            skill_id=snapshot["skill"]["id"], skill_version=snapshot["skill"]["version"],
            context_snapshot=snapshot, approval_id=approval_id, started_at=now,
            deadline_at=now + timedelta(seconds=snapshot.get("max_run_seconds", get_settings().max_run_seconds)),
            cancel_requested=cancelled, status="cancel_requested" if cancelled else "running",
            cancel_requested_by_employee_id=employee_id if cancelled else None,
        ))


def cancellation_requested(run_id: UUID) -> bool:
    with SessionFactory() as session:
        row = session.execute(select(AgentRunRecord.cancel_requested, AgentRunRecord.status)
                              .where(AgentRunRecord.run_id == run_id)).one_or_none()
        # 父执行结束后，迟到的子线程也必须停止，不再发起新的模型或工具调用。
        return bool(row and (row.cancel_requested or row.status not in {"running", "cancel_requested"}))


def request_cancel(run_id: UUID, employee_id: UUID) -> RunView:
    # 不获取会话执行锁，运行中的工作线程才能及时观察到取消标记。
    with SessionFactory.begin() as session:
        record = session.scalar(select(AgentRunRecord).where(AgentRunRecord.run_id == run_id, scope(employee_id))
                                .with_for_update())
        if record is None:
            raise RunNotFound("未找到该执行记录。")
        if record.status not in {"running", "cancel_requested", "cancelled"}:
            raise RunConflict("该执行已结束；待审批任务请使用审批取消接口。")
        if record.status != "cancelled":
            record.cancel_requested = True
            record.cancel_requested_by_employee_id = employee_id
            record.status = "cancel_requested"
        session.flush()
        return RunView.model_validate(record)


def charge(run_id: UUID, turn_id: UUID, resource: str, count: int, limit: int, baseline: int = 0,
           *, step_id: str | None = None, step_limit: int | None = None) -> None:
    column = AgentRunRecord.model_calls if resource == "model_calls" else AgentRunRecord.tool_calls
    with SessionFactory.begin() as session:
        record = session.get(AgentRunRecord, run_id, with_for_update=True)
        if record.status not in {"running", "cancel_requested"}:
            raise ExecutionCancelled("父执行已经结束，停止子任务调用。")
        used = session.scalar(select(func.coalesce(func.sum(column), 0)).where(AgentRunRecord.turn_id == turn_id)) + baseline
        if used + count > limit:
            raise ExecutionBudgetExceeded(resource, limit, used, count)
        if step_id is not None:
            snapshots = session.scalars(select(AgentRunRecord.context_snapshot).where(AgentRunRecord.turn_id == turn_id)).all()
            step_used = sum(item.get("step_usage", {}).get(step_id, {}).get(resource, 0) for item in snapshots)
            if step_limit is None or step_used + count > step_limit:
                raise ExecutionBudgetExceeded(resource, step_limit or 0, step_used, count)
            # 失败调用也记入步骤预算，跨 run 恢复时与整轮预算一起累计。
            usage = dict(record.context_snapshot.get("step_usage", {}))
            current = dict(usage.get(step_id, {}))
            current[resource] = current.get(resource, 0) + count
            usage[step_id] = current
            record.context_snapshot = {**record.context_snapshot, "step_usage": usage}
        setattr(record, resource, getattr(record, resource) + count)


def finish_run(run_id: UUID, status: str, *, error_type: str | None = None,
               approval_id: UUID | None = None) -> None:
    with SessionFactory.begin() as session:
        record = session.get(AgentRunRecord, run_id, with_for_update=True)
        record.status = status
        record.finished_at = datetime.now(timezone.utc)
        record.error_type = error_type[:64] if error_type else None
        if approval_id is not None:
            record.approval_id = approval_id
