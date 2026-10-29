from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ContextCapacityExceeded(Exception):
    """当前轮或固定提示超出容量，不能通过删减历史安全解决。"""


class ContextDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["extractive_v1"] = "extractive_v1"
    revision: int = Field(default=0, ge=0)
    covered_messages: int = Field(default=0, ge=0)
    covered_message_id: str = ""
    covered_turns: int = Field(default=0, ge=0)
    dropped_turns: int = Field(default=0, ge=0)
    dropped_details: int = Field(default=0, ge=0)
    user_conditions: dict[str, dict] = Field(default_factory=dict)
    turns: list[dict] = Field(default_factory=list)
    vehicle_selections: list[dict] = Field(default_factory=list)
    operations: list[dict] = Field(default_factory=list)


class ContextUsage(BaseModel):
    estimator: Literal["utf8_bytes_upper_estimate"] = "utf8_bytes_upper_estimate"
    original_estimated_tokens: int
    input_estimated_tokens: int
    input_limit: int
    output_reserved_tokens: int
    retained_messages: int
    covered_messages: int
    summary_revision: int


class ConversationContextView(BaseModel):
    total_messages: int
    digest: ContextDigest | None = None
    usage: ContextUsage | None = None
