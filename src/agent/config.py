from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用运行配置。"""

    model_config = SettingsConfigDict(
        env_prefix="CHEXU_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    mode: Literal["live"] = "live"
    model_base_url: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    model_api_key: SecretStr = Field(min_length=1, exclude=True)
    model_use_env_proxy: bool = True
    database_url: SecretStr = Field(min_length=1, exclude=True)
    database_schema: str = Field(default="public", pattern=r"^[a-z][a-z0-9_]{0,62}$")
    checkpoint_schema: str = Field(default="chexu_agent", pattern=r"^[a-z][a-z0-9_]{0,62}$")
    max_model_calls: int = Field(default=4, ge=1, le=20)
    max_tool_calls: int = Field(default=6, ge=1, le=50)
    access_token_ttl_minutes: int = Field(default=480, ge=5, le=1440)
    login_max_failures: int = Field(default=5, ge=2, le=20)
    login_lock_minutes: int = Field(default=15, ge=1, le=60)
    approval_ttl_minutes: int = Field(default=1440, ge=5, le=10080)
    approval_max_resume_attempts: int = Field(default=3, ge=1, le=10)
    max_run_seconds: float = Field(default=60, gt=0, le=600)
    model_timeout_seconds: float = Field(default=20, gt=0, le=120)
    tool_timeout_seconds: float = Field(default=5, gt=0, le=60)
    db_lock_timeout_seconds: float = Field(default=2, gt=0, le=30)
    max_run_resumes: int = Field(default=3, ge=1, le=10)
    report_storage_dir: Path = PROJECT_ROOT / "data" / "reports"
    max_report_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    max_report_text_chars: int = Field(default=60000, ge=1600, le=200000)
    report_mcp_timeout_seconds: float = Field(default=20, gt=0, le=120)
    task_worker_enabled: bool = True
    task_queue_name: str = Field(default="chexu", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    task_workers: int = Field(default=1, ge=1, le=4)
    task_poll_seconds: float = Field(default=0.5, ge=0.1, le=5)
    task_max_pending_per_employee: int = Field(default=5, ge=1, le=50)
    task_stream_seconds: float = Field(default=30, ge=1, le=120)
    plan_max_steps: int = Field(default=6, ge=1, le=6)
    plan_max_model_calls: int = Field(default=16, ge=2, le=20)
    plan_max_tool_calls: int = Field(default=24, ge=1, le=50)
    plan_max_run_seconds: float = Field(default=180, gt=0, le=600)
    plan_max_parallel: int = Field(default=2, ge=1, le=2)
    plan_max_replans: int = Field(default=1, ge=0, le=3)
    plan_max_revisions: int = Field(default=5, ge=1, le=10)
    subagent_handoff_max_chars: int = Field(default=16000, ge=2000, le=32000)
    subagent_context_max_chars: int = Field(default=48000, ge=4000, le=100000)
    context_window_tokens: int = Field(default=32000, ge=8000, le=200000)
    context_output_tokens: int = Field(default=2000, ge=256, le=16000)
    context_summary_tokens: int = Field(default=6000, ge=1024, le=16000)
    context_compact_ratio: float = Field(default=0.75, ge=0.4, le=0.95)
    context_recent_turns: int = Field(default=4, ge=1, le=20)
    tavily_api_key: SecretStr | None = Field(default=None, exclude=True)
    tavily_timeout_seconds: float = Field(default=10, gt=0, le=30)
    tavily_max_results: int = Field(default=5, ge=1, le=5)
    tavily_use_env_proxy: bool = True

    @model_validator(mode="after")
    def validate_context_budget(self):
        available = self.context_window_tokens - self.context_output_tokens
        if available < 4096 or self.context_summary_tokens >= available // 2:
            raise ValueError("上下文预算必须留出至少 4096 输入容量，摘要预算需小于输入容量的一半。")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
