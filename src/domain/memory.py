from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, computed_field, model_validator


MemoryCategory = Literal["budget", "usage", "preferences"]
MemoryValue = StrictInt | StrictStr | list[StrictStr]


class MemoryToolError(Exception):
    pass


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: MemoryCategory
    value: MemoryValue

    @model_validator(mode="after")
    def validate_value(self):
        if self.category == "budget":
            if type(self.value) is not int or not 1 <= self.value <= 2_147_483_647:
                raise ValueError("预算必须是正整数，单位为人民币元。")
        elif self.category == "usage":
            if not isinstance(self.value, str) or not 1 <= len(self.value.strip()) <= 200:
                raise ValueError("用途必须是 1～200 字的文本。")
            self.value = self.value.strip()
        else:
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 10:
                raise ValueError("偏好必须是包含 1～10 条文本的数组。")
            self.value = list(dict.fromkeys(item.strip() for item in self.value))
            if any(not 1 <= len(item) <= 100 for item in self.value):
                raise ValueError("每条偏好必须为 1～100 字。")
        if "\x00" in str(self.value):
            raise ValueError("记忆内容不能含空字符。")
        return self


class MemoryCreate(MemoryInput):
    confirm: Literal[True]


class MemoryConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]
    expected_revision: int = Field(strict=True, ge=1)


class MemoryUpdate(MemoryConfirm):
    value: MemoryValue


class MemoryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    memory_id: UUID
    category: MemoryCategory
    value: MemoryValue
    status: Literal["pending", "confirmed"]
    source: Literal["user_entered", "agent_proposed"]
    source_quote: str | None
    source_thread_id: UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None

    @computed_field
    @property
    def requires_confirmation(self) -> bool:
        return self.status == "pending"

    @computed_field
    @property
    def evidence_level(self) -> Literal["user_confirmed", "unconfirmed_candidate"]:
        return "user_confirmed" if self.status == "confirmed" else "unconfirmed_candidate"
