from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text

from src.agent.config import get_settings
from src.database.connection import SessionFactory
from src.database.models import AgentRunRecord, AgentTaskRecord, ApprovalRecord, EmployeeRecord, PlanCommandRecord
from src.domain.planning import PlanActionInput, PlanActionView
from src.domain.tasks import ACTIVE_TASK_STATUSES
from src.services.conversations import ConversationAccessError, owned_conversation


class PlanActionConflict(Exception):
    pass


def approval_locked(session, thread_id):
    return session.scalar(select(ApprovalRecord.approval_id).where(ApprovalRecord.thread_id == thread_id,
        or_(ApprovalRecord.status.in_(["pending", "approved"]), ApprovalRecord.quote_id.is_not(None),
            ApprovalRecord.workflow_completed.is_(False))).limit(1)) is not None


def is_approval_locked(thread_id):
    with SessionFactory() as session:
        return approval_locked(session, thread_id)


def get_action(thread_id: UUID, command_id: UUID, employee_id: UUID) -> PlanActionView:
    with SessionFactory() as session:
        owned_conversation(session, thread_id, employee_id)
        record = session.get(PlanCommandRecord, command_id)
        if record is None or record.thread_id != thread_id or record.employee_id != employee_id:
            raise ConversationAccessError("未找到该计划调整指令。")
        return PlanActionView.model_validate(record)


def submit(thread_id: UUID, employee_id: UUID, body: PlanActionInput) -> PlanActionView:
    from src.agent.vehicle_agent import build_vehicle_agent
    from src.database.checkpoints import open_checkpointer
    from src.agent.runtime import pending_approval_id

    payload = {"message": body.message.strip(), "restart_from": body.restart_from}
    if not payload["message"]:
        raise PlanActionConflict("请填写补充信息或调整目标。")
    with SessionFactory() as session:
        owned_conversation(session, thread_id, employee_id)
        old = session.get(PlanCommandRecord, body.command_id)
        if old is not None:
            if old.thread_id != thread_id or old.employee_id != employee_id:
                raise ConversationAccessError("未找到该计划调整指令。")
            if (old.action != body.action or old.expected_revision != body.expected_revision
                    or old.turn_id != body.expected_turn_id or old.payload != payload):
                raise PlanActionConflict("同一 command_id 不能提交不同的调整内容。")
            return PlanActionView.model_validate(old)
    with open_checkpointer() as saver:
        state = build_vehicle_agent(saver).get_state({"configurable": {"thread_id": str(thread_id)}}, subgraphs=True)
    plan = state.values.get("plan")
    if not plan or plan.get("status") == "cancelled":
        raise PlanActionConflict("当前没有可调整的计划，请发起新任务。")
    if pending_approval_id(state):
        raise PlanActionConflict("该计划正在处理审批，请先完成或取消审批。")
    if state.values.get("turn_id") != str(body.expected_turn_id):
        raise PlanActionConflict("会话已进入另一轮任务，请读取最新计划后再提交。")
    if plan.get("revision", 1) != body.expected_revision:
        raise PlanActionConflict("计划版本已变化，请读取最新进度后再提交。")
    if body.action == "continue" and plan["status"] != "needs_input":
        raise PlanActionConflict("只有待补充资料的计划可以续接；其他调整请使用 revise 或 replan。")
    if body.action == "replan" and all(step["status"] == "completed" for step in plan["steps"]):
        raise PlanActionConflict("全部步骤已经完成；修改目标请使用 revise，或开启新任务。")
    if body.restart_from and (body.action != "revise" or body.restart_from not in {step["id"] for step in plan["steps"]}):
        raise PlanActionConflict("restart_from 必须指向 revise 计划中的已有步骤。")
    with SessionFactory.begin() as session:
        owned_conversation(session, thread_id, employee_id)
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                        {"key": f"chexu-plan-command:{employee_id}"})
        count = session.scalar(select(func.count()).select_from(PlanCommandRecord).where(
            PlanCommandRecord.employee_id == employee_id, PlanCommandRecord.status == "queued"))
        if count >= get_settings().task_max_pending_per_employee:
            raise PlanActionConflict("待处理的计划调整已达上限，请等待完成后再提交。")
        if approval_locked(session, thread_id):
            raise PlanActionConflict("该计划正在审批或已保存确认报价，请先处理审批，或开启新任务。")
        active = session.scalar(select(AgentTaskRecord).where(AgentTaskRecord.thread_id == thread_id,
            AgentTaskRecord.status.in_(ACTIVE_TASK_STATUSES)))
        record = PlanCommandRecord(command_id=body.command_id, thread_id=thread_id, employee_id=employee_id,
            task_id=active.task_id if active else None, queue_name=active.queue_name if active else get_settings().task_queue_name,
            action=body.action, expected_revision=body.expected_revision, turn_id=body.expected_turn_id, payload=payload, status="queued")
        # 续接原后台任务时沿用它的队列，避免开发/测试队列相互领取。
        if active is None:
            task_id = (state.values.get("context_snapshot") or {}).get("task_id")
            previous = session.get(AgentTaskRecord, UUID(task_id)) if task_id else None
            if previous and previous.employee_id == employee_id:
                record.queue_name = previous.queue_name
        session.add(record)
        session.flush()
        queue_name = record.queue_name
    schedule_ready(queue_name=queue_name, thread_id=thread_id)
    return get_action(thread_id, body.command_id, employee_id)


def pending(thread_id: UUID) -> UUID | None:
    with SessionFactory() as session:
        return session.scalar(select(PlanCommandRecord.command_id).where(PlanCommandRecord.thread_id == thread_id,
            PlanCommandRecord.status == "queued").order_by(PlanCommandRecord.created_at, PlanCommandRecord.command_id).limit(1))


def read(command_id: UUID) -> dict:
    with SessionFactory() as session:
        record = session.get(PlanCommandRecord, command_id)
        if record is None:
            raise PlanActionConflict("计划调整指令已不可用。")
        return {"command_id": str(record.command_id), "thread_id": str(record.thread_id), "employee_id": str(record.employee_id),
                "action": record.action, "expected_revision": record.expected_revision, "payload": record.payload,
                "turn_id": str(record.turn_id),
                "status": record.status}


def finish(command_id: UUID, status: str, *, task_id: UUID | None = None, error_code: str | None = None):
    with SessionFactory.begin() as session:
        record = session.get(PlanCommandRecord, command_id, with_for_update=True)
        if record is None or record.status in {"applied", "rejected"}:
            return
        record.status, record.error_code = status, error_code
        record.finished_at = datetime.now(timezone.utc)
        if task_id:
            record.task_id = task_id


def reject_pending(thread_id: UUID, code: str):
    with SessionFactory.begin() as session:
        records = session.scalars(select(PlanCommandRecord).where(PlanCommandRecord.thread_id == thread_id,
            PlanCommandRecord.status == "queued").with_for_update()).all()
        for record in records:
            record.status, record.error_code, record.finished_at = "rejected", code, datetime.now(timezone.utc)


def schedule_ready(*, queue_name: str, thread_id: UUID | None = None) -> int:
    from src.services.tasks import _append_event

    with SessionFactory() as session:
        statement = select(PlanCommandRecord.command_id).where(PlanCommandRecord.queue_name == queue_name,
            PlanCommandRecord.status == "queued")
        if thread_id:
            statement = statement.where(PlanCommandRecord.thread_id == thread_id)
        candidates = session.scalars(statement.order_by(PlanCommandRecord.created_at).limit(20)).all()
    scheduled = 0
    for command_id in candidates:
        # 每个事务只调度一个会话，避免多个 worker 交叉持有不同员工/会话的锁。
        with SessionFactory.begin() as session:
            command = session.scalar(select(PlanCommandRecord).where(PlanCommandRecord.command_id == command_id,
                PlanCommandRecord.status == "queued").with_for_update(skip_locked=True))
            if command is None:
                continue
            acquired = session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"chexu-chat:{command.thread_id}"})
            if not acquired:
                continue
            active = session.scalar(select(AgentTaskRecord.task_id).where(AgentTaskRecord.thread_id == command.thread_id,
                AgentTaskRecord.status.in_(ACTIVE_TASK_STATUSES)))
            if active:
                continue
            source = session.scalar(select(AgentRunRecord).where(AgentRunRecord.thread_id == command.thread_id)
                .order_by(AgentRunRecord.started_at.desc()).limit(1))
            if source is None:
                continue
            if source.status in {"running", "cancel_requested"}:
                if source.deadline_at > datetime.now(timezone.utc):
                    continue
                source.status, source.error_type = "failed", "WorkerInterrupted"
                source.finished_at = datetime.now(timezone.utc)
            if source.status == "cancelled":
                command.status, command.error_code, command.finished_at = "rejected", "plan_cancelled", datetime.now(timezone.utc)
                continue
            if source.skill_id != "deal_review" or source.turn_id != command.turn_id:
                command.status, command.error_code, command.finished_at = "rejected", "plan_changed", datetime.now(timezone.utc)
                continue
            if approval_locked(session, command.thread_id):
                command.status, command.error_code = "rejected", "approval_locked"
                command.finished_at = datetime.now(timezone.utc)
                continue
            employee = session.get(EmployeeRecord, command.employee_id, with_for_update=True)
            if employee is None or not employee.is_active:
                command.status, command.error_code = "rejected", "employee_inactive"
                command.finished_at = datetime.now(timezone.utc)
                continue
            count = session.scalar(select(func.count()).select_from(AgentTaskRecord).where(
                AgentTaskRecord.employee_id == command.employee_id, AgentTaskRecord.status.in_(ACTIVE_TASK_STATUSES)))
            if count >= get_settings().task_max_pending_per_employee:
                continue
            original_id = source.context_snapshot.get("task_id")
            task = session.get(AgentTaskRecord, UUID(original_id), with_for_update=True) if original_id else None
            if task and (task.employee_id != command.employee_id or task.thread_id != command.thread_id):
                command.status, command.error_code, command.finished_at = "rejected", "ownership_changed", datetime.now(timezone.utc)
                continue
            if task is None:
                task = AgentTaskRecord(task_id=uuid4(), thread_id=command.thread_id, employee_id=command.employee_id,
                    message="计划调整", queue_name=queue_name, skill_id="deal_review", event_sequence=0)
                session.add(task)
            task.run_id, task.status, task.operation, task.command_id = uuid4(), "queued", "plan_action", command.command_id
            task.cancel_requested = False
            task.result, task.error_code, task.error_message = None, None, None
            task.worker_token, task.lease_until, task.finished_at = None, None, None
            session.flush()
            command.task_id = task.task_id
            _append_event(session, task, "task.queued", {"status": "queued", "command_id": str(command.command_id)}, task.run_id)
            scheduled += 1
    return scheduled
