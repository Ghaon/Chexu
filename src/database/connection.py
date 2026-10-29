from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.agent.config import get_settings


settings = get_settings()

engine = create_engine(
    settings.database_url.get_secret_value(),
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5, "options": (
        f"-c search_path={settings.database_schema} -c statement_timeout={int(settings.tool_timeout_seconds * 1000)} "
        f"-c lock_timeout={int(settings.db_lock_timeout_seconds * 1000)}"
    )},
    hide_parameters=True,
)

# 复用会话工厂，每次数据库操作创建独立的 Session。
SessionFactory = sessionmaker(bind=engine)
