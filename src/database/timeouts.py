from sqlalchemy import text

from src.agent.config import get_settings


def apply_query_timeout(session, timeout_ms: int | None = None) -> None:
    settings = get_settings()
    statement_ms = timeout_ms if timeout_ms is not None else int(settings.tool_timeout_seconds * 1000)
    lock_ms = min(statement_ms, int(settings.db_lock_timeout_seconds * 1000))
    # SET LOCAL 随事务结束自动还原，不会把本次执行的配置泄漏到连接池中的下一次请求。
    session.execute(text("SELECT set_config('statement_timeout', :statement_ms, true), "
                         "set_config('lock_timeout', :lock_ms, true)"),
                    {"statement_ms": str(max(1, statement_ms)), "lock_ms": str(max(1, lock_ms))})
