import sys

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.database.connection import SessionFactory, engine


def main() -> int:
    try:
        with SessionFactory() as session:
            result = session.execute(
                text("SELECT current_database() AS database_name, current_user AS user_name")
            ).one()

        print(f"Database connected: {result.database_name} (user: {result.user_name})")
        return 0
    except SQLAlchemyError as exc:
        # 连接错误可能包含认证信息，命令行只输出异常类型。
        print(
            f"Database connection failed ({type(exc).__name__}). "
            "Check PostgreSQL status and CHEXU_DATABASE_URL.",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
