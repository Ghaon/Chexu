from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from src.agent.config import get_settings
from src.database.connection import SessionFactory
from src.database.models import ApprovalRecord, EmployeeRecord, QuoteRecord, VehicleRecord
from src.domain.approvals import ApprovalTask
from src.domain.quotes import QuoteDraft
from src.services.conversations import owned_conversation
from src.database.timeouts import apply_query_timeout


class ApprovalNotFound(Exception):
    pass


class ApprovalConflict(Exception):
    pass


def get_task(approval_id: UUID, employee_id: UUID | None = None, manager: bool = False) -> ApprovalTask:
    with SessionFactory() as session:
        record = session.get(ApprovalRecord, approval_id)
        if record is None or (employee_id is not None and not manager
                              and record.requested_by_employee_id != employee_id):
            raise ApprovalNotFound("未找到该审批任务。")
        return ApprovalTask.model_validate(record)


def prepare_task(thread_id: UUID, employee_id: UUID, request_key: str, draft: QuoteDraft,
                 *, timeout_ms: int | None = None) -> ApprovalTask:
    with SessionFactory.begin() as session:
        apply_query_timeout(session, timeout_ms)
        owned_conversation(session, thread_id, employee_id)
        record = session.scalar(select(ApprovalRecord).where(
            ApprovalRecord.thread_id == thread_id, ApprovalRecord.request_key == request_key,
        ))
        if record is not None:
            if record.requested_by_employee_id != employee_id or record.quote_draft != draft.model_dump(mode="json"):
                raise ApprovalConflict("审批请求与原报价不一致，请重新提交。")
            return ApprovalTask.model_validate(record)
        employee = session.get(EmployeeRecord, employee_id, with_for_update=True)
        vehicle = session.scalar(select(VehicleRecord).where(VehicleRecord.vehicle_id == draft.vehicle_id))
        if employee is None or not employee.is_active or vehicle is None:
            raise ApprovalConflict("员工或车辆资料已不可用，无法提交审批。")
        record = ApprovalRecord(
            thread_id=thread_id, request_key=request_key,
            requested_by_employee_id=employee_id, requested_by_username=employee.username,
            vehicle_record_id=vehicle.id, quote_draft=draft.model_dump(mode="json"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=get_settings().approval_ttl_minutes),
        )
        session.add(record)
        session.flush()
        return ApprovalTask.model_validate(record)


def decide_task(approval_id: UUID, actor_id: UUID, decision: str, note: str = "") -> ApprovalTask:
    with SessionFactory.begin() as session:
        actor = session.get(EmployeeRecord, actor_id, with_for_update=True)
        record = session.get(ApprovalRecord, approval_id, with_for_update=True)
        if record is None:
            raise ApprovalNotFound("未找到该审批任务。")
        if actor is None or not actor.is_active:
            raise ApprovalConflict("当前员工已停用，无法处理审批。")
        if decision in {"approve", "reject"} and actor.role != "manager":
            raise ApprovalConflict("只有经理可以审核报价。")
        if decision == "cancel" and actor.role != "manager" and record.requested_by_employee_id != actor_id:
            raise ApprovalNotFound("未找到该审批任务。")
        if record.status != "pending":
            if record.decision != decision and record.status != "expired":
                raise ApprovalConflict("该审批任务已处理，不能改为另一种决定。")
            return ApprovalTask.model_validate(record)
        now = datetime.now(timezone.utc)
        record.decided_at = now
        if record.expires_at <= now:
            record.status = "expired"
        else:
            record.decision = decision
            record.decided_by_employee_id = actor_id
            record.review_note = note
            record.status = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[decision]
        session.flush()
        return ApprovalTask.model_validate(record)


def reserve_resume(approval_id: UUID) -> None:
    with SessionFactory.begin() as session:
        record = session.get(ApprovalRecord, approval_id, with_for_update=True)
        if record.resume_attempts >= get_settings().approval_max_resume_attempts:
            raise ApprovalConflict("审批恢复次数已达上限，请查看已保存报价或重新开启会话。")
        record.resume_attempts += 1
        record.last_resume_error = None


def finish_resume(approval_id: UUID, *, completed: bool, error_type: str | None = None) -> ApprovalTask:
    with SessionFactory.begin() as session:
        record = session.get(ApprovalRecord, approval_id, with_for_update=True)
        record.workflow_completed = completed
        record.last_resume_error = error_type[:64] if error_type else None
        if error_type and record.status == "approved" and record.resume_attempts >= get_settings().approval_max_resume_attempts:
            record.status = "failed"
            record.failure_reason = "审批执行重试次数已达上限，请重新提交。"
        session.flush()
        return ApprovalTask.model_validate(record)


def resolve_task(approval_id: UUID, *, timeout_ms: int | None = None) -> ApprovalTask:
    """在一个事务内完成报价写入和审批结果，重放时返回已有报价。"""
    with SessionFactory.begin() as session:
        apply_query_timeout(session, timeout_ms)
        record = session.get(ApprovalRecord, approval_id)
        if record is None:
            raise ApprovalNotFound("未找到该审批任务。")
        if record.status == "pending":
            raise ApprovalConflict("审批尚未由有权限的员工处理，不能执行报价。")
        if record.status != "approved":
            return ApprovalTask.model_validate(record)
        # 与普通报价接口一致：先锁全部关联员工，再锁审批和车辆。
        # 多个员工统一按编号排序，避免外键检查与其他保存事务形成循环等待。
        people = session.scalars(select(EmployeeRecord).where(EmployeeRecord.employee_id.in_([
            record.decided_by_employee_id, record.requested_by_employee_id,
        ])).order_by(EmployeeRecord.employee_id).with_for_update()).all()
        people = {employee.employee_id: employee for employee in people}
        session.refresh(record, with_for_update=True)
        if record.status != "approved":
            return ApprovalTask.model_validate(record)
        manager = people.get(record.decided_by_employee_id)
        requester = people.get(record.requested_by_employee_id)
        vehicle = session.get(VehicleRecord, record.vehicle_record_id, with_for_update=True)
        draft = QuoteDraft.model_validate(record.quote_draft)
        reason = None
        if manager is None or not manager.is_active or manager.role != "manager":
            reason = "审批人已停用或权限已变化，请重新提交。"
        elif requester is None or not requester.is_active:
            reason = "发起员工已停用，未保存报价。"
        elif vehicle is None or vehicle.price_cny != draft.listed_price_cny:
            reason = "车辆售价已变化，请重新生成报价并提交审批。"
        elif vehicle.title != draft.vehicle_title or vehicle.is_demo != draft.is_demo:
            reason = "车辆资料已变化，请重新生成报价并提交审批。"
        elif draft.quoted_price_cny != draft.listed_price_cny - draft.discount_cny or draft.discount_cny > draft.listed_price_cny:
            reason = "报价金额校验失败，请重新提交。"
        if reason:
            record.status = "failed"
            record.failure_reason = reason
        else:
            quote = QuoteRecord(vehicle_record_id=vehicle.id, **(draft.model_dump() | {
                "status": "confirmed", "requires_confirmation": False,
                "confirmed_at": datetime.now(timezone.utc),
                "created_by_employee_id": requester.employee_id,
                "confirmed_by_employee_id": manager.employee_id,
            }))
            session.add(quote)
            session.flush()
            record.quote_id = quote.quote_id
            record.status = "completed"
        session.flush()
        return ApprovalTask.model_validate(record)


def abort_execution(thread_id: UUID, employee_id: UUID, request_key: str) -> ApprovalTask | None:
    """取消执行时收尾当前提案；已经提交的报价保持原事实。"""
    with SessionFactory.begin() as session:
        apply_query_timeout(session)
        session.get(EmployeeRecord, employee_id, with_for_update=True)
        record = session.scalar(select(ApprovalRecord).where(
            ApprovalRecord.thread_id == thread_id,
            ApprovalRecord.requested_by_employee_id == employee_id,
            ApprovalRecord.request_key == request_key,
        ).order_by(ApprovalRecord.created_at.desc()).limit(1).with_for_update())
        if record is None:
            return None
        if record.status == "pending":
            record.status = "cancelled"
            record.decision = "cancel"
            record.decided_by_employee_id = employee_id
            record.decided_at = datetime.now(timezone.utc)
            record.review_note = "执行已取消。"
        elif record.status == "approved":
            record.status = "failed"
            record.failure_reason = "审核后的执行已取消，未保存报价。"
        record.workflow_completed = True
        session.flush()
        return ApprovalTask.model_validate(record)
