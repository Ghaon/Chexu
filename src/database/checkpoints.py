from contextlib import contextmanager
import logging

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import Connection, Error
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from src.agent.config import get_settings


logger = logging.getLogger(__name__)


class ConversationBusyError(Exception):
    pass


@contextmanager
def open_checkpointer(thread_id: str | None = None):
    settings = get_settings()
    url = make_url(settings.database_url.get_secret_value())
    conninfo = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with Connection.connect(
        conninfo,
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=0,
        connect_timeout=5,
        options=f"-c search_path={settings.checkpoint_schema} -c statement_timeout=5000 -c lock_timeout=2000",
    ) as connection:
        if thread_id is not None:
            # 锁覆盖整轮执行；连接关闭时释放，防止同一会话并发覆盖检查点。
            locked = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                (f"chexu-chat:{thread_id}",),
            ).fetchone()["acquired"]
            if not locked:
                raise ConversationBusyError("该会话正在处理上一条消息，请稍后再发。")
        yield PostgresSaver(
            connection,
            serde=JsonPlusSerializer(allowed_msgpack_modules=[]),
        )


def main() -> int:
    try:
        with open_checkpointer() as checkpointer:
            checkpointer.conn.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (f"chexu-checkpoint-setup:{get_settings().checkpoint_schema}",),
            )
            from psycopg import sql
            checkpointer.conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(get_settings().checkpoint_schema)))
            checkpointer.setup()
    except Error as exc:
        logger.error("Checkpoint setup failed (%s).", type(exc).__name__)
        return 1
    print(f"Conversation checkpoint tables are ready in schema {get_settings().checkpoint_schema}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
