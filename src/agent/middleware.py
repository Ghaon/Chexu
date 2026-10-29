from collections.abc import Callable, Sequence
import logging

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCallRequest

from src.agent.budget import ExecutionBudget, ExecutionBudgetExceeded
from src.agent.config import get_settings
from src.agent.context import AgentContext
from src.agent.skills import TOOL_ROLES
from src.agent.tools.vehicle_tools import VehicleToolError
from src.agent.tracing import emit_context_event
from src.domain.execution import ExecutionCancelled, ExecutionTimedOut
from src.domain.memory import MemoryToolError
from src.domain.web_search import WebSearchError


logger = logging.getLogger(__name__)
ToolHandler = Callable[[dict, RunnableConfig], dict]
ToolPolicy = Callable[[dict, RunnableConfig, ToolHandler, AgentContext], dict]


def handle_tool_error(error: Exception) -> str:
    # 运行控制信号必须上抛给 runtime，不能变成让模型继续重试的普通工具错误。
    if isinstance(error, (ExecutionCancelled, ExecutionTimedOut, ExecutionBudgetExceeded)):
        raise error
    if isinstance(error, (VehicleToolError, MemoryToolError, WebSearchError)):
        return str(error)
    logger.error("Unexpected tool failure (%s).", type(error).__name__)
    return "工具处理失败，请稍后重试。"


class ToolExecutionMiddleware:
    """工具调用的公共执行边界；业务校验通过 policy 扩展。"""

    def __init__(self, tools: Sequence[BaseTool], context: AgentContext | None, budget: ExecutionBudget):
        self.context, self.budget = context, budget
        self.names = frozenset(tool.name for tool in tools)
        self._node = ToolNode(tools, handle_tool_errors=handle_tool_error, wrap_tool_call=self._checked_call)

    def _bounded_config(self, config: RunnableConfig) -> RunnableConfig:
        context, settings = self.context, get_settings()
        return {**config, "configurable": {**config.get("configurable", {}),
                "run_id": str(context.run_id), "thread_id": str(context.thread_id),
                "employee_id": str(context.employee_id),
                "tool_timeout_ms": max(1, int(context.control.timeout(settings.tool_timeout_seconds) * 1000)),
                "mcp_timeout_ms": max(1, int(context.control.timeout(settings.report_mcp_timeout_seconds) * 1000)),
                "search_timeout_ms": max(1, int(context.control.timeout(settings.tavily_timeout_seconds) * 1000)),
                "run_deadline": context.control.deadline}}

    def _checked_call(self, request: ToolCallRequest, execute):
        # ToolNode 为每个调用分配独立 config；排队后的调用重新计算剩余时间。
        config = request.runtime.config
        config["configurable"] = self._bounded_config(config)["configurable"]
        try:
            return execute(request)
        finally:
            self.context.control.check()

    def invoke(self, state: dict, config: RunnableConfig, *, policy: ToolPolicy | None = None) -> dict:
        context = self.context
        if context is None:
            raise RuntimeError("执行工具必须由 runtime 提供运行上下文。")
        calls = state["messages"][-1].tool_calls
        try:
            context.control.check()
            if any(call["name"] not in self.names or call["name"] not in context.skill.tools
                   or context.role not in TOOL_ROLES.get(call["name"], ()) for call in calls):
                emit_context_event("tool.batch.rejected", context, error_code="tool_permission_denied",
                                   tool_call_count=len(calls), status="rejected")
                return {"messages": [ToolMessage(
                    content="当前技能或身份无权调用该批工具，请使用允许的工具或更换技能。",
                    name=call["name"], tool_call_id=call["id"], status="error",
                    additional_kwargs={"error_code": "tool_permission_denied"}) for call in calls],
                    "tool_calls_used": self.budget.tool_calls}
            # 先为整批调用预留持久化预算，再启动 ToolNode 的并行调用。
            context.control.charge("tool_calls", len(calls), self.budget.max_tool_calls)
            self.budget.consume_tools(len(calls))
            bounded = self._bounded_config(config)
            result = (policy(state, bounded, self._node.invoke, context) if policy is not None
                      else self._node.invoke(state, config=bounded))
            context.control.check()
            return result | {"tool_calls_used": self.budget.tool_calls}
        except ExecutionBudgetExceeded as exc:
            emit_context_event("tool.batch.rejected", context, error_code="tool_budget_exceeded", status="rejected",
                               resource=exc.resource, limit=exc.limit, used=exc.used, requested=exc.requested)
            raise
        except (ExecutionCancelled, ExecutionTimedOut) as exc:
            emit_context_event("tool.batch.interrupted", context, error_type=type(exc).__name__,
                               status="cancelled" if isinstance(exc, ExecutionCancelled) else "timed_out")
            raise
