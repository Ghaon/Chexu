from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from src.database.connection import SessionFactory
from src.database.models import ConversationRecord


class ConversationAccessError(Exception):
    pass


def owned_conversation(session: Session, thread_id: UUID, employee_id: UUID) -> ConversationRecord:
    record = session.get(ConversationRecord, thread_id)
    if record is None or record.employee_id != employee_id:
        raise ConversationAccessError("未找到该会话。")
    return record


def ensure_conversation(thread_id: str, employee_id: UUID, message: str, has_checkpoint: bool) -> None:
    """在持有会话执行锁期间检查归属，或创建一段全新会话。"""
    with SessionFactory.begin() as session:
        record = session.get(ConversationRecord, UUID(thread_id))
        if record is None:
            # 老检查点没有可验证的归属，不能由首次提交该编号的人认领。
            if has_checkpoint:
                raise ConversationAccessError("未找到该会话，请开启新会话。")
            record = ConversationRecord(thread_id=UUID(thread_id), employee_id=employee_id,
                                        title=message.strip()[:100] or "新会话")
            session.add(record)
        elif record.employee_id != employee_id:
            raise ConversationAccessError("未找到该会话。")
        record.updated_at = datetime.now(timezone.utc)
