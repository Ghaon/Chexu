from dataclasses import dataclass, field
from threading import Lock


class ExecutionBudgetExceeded(Exception):
    def __init__(self, resource: str, limit: int, used: int, requested: int):
        self.resource = resource
        self.limit = limit
        self.used = used
        self.requested = requested
        label = "模型" if resource == "model_calls" else "工具"
        super().__init__(f"本轮剩余{label}调用额度不足，请缩小问题范围后开启新会话。")


@dataclass
class ExecutionBudget:
    max_model_calls: int
    max_tool_calls: int
    model_calls: int = 0
    tool_calls: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def consume_model(self) -> None:
        with self._lock:
            if self.model_calls + 1 > self.max_model_calls:
                raise ExecutionBudgetExceeded("model_calls", self.max_model_calls, self.model_calls, 1)
            self.model_calls += 1

    def consume_tools(self, count: int) -> None:
        # 在 ToolNode 并发执行前为整批调用预留额度，避免执行一半才超限。
        with self._lock:
            if self.tool_calls + count > self.max_tool_calls:
                raise ExecutionBudgetExceeded("tool_calls", self.max_tool_calls, self.tool_calls, count)
            self.tool_calls += count
