from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from threading import Lock
from time import perf_counter

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage

from src.agent.budget import ExecutionBudgetExceeded

logger = logging.getLogger("chexu.agent.trace")

TOOL_ARGUMENTS = {
    "get_vehicle": {"vehicle_id"},
    "search_vehicles": {"min_price_cny", "max_price_cny", "max_mileage_km", "limit"},
    "create_quote_draft": {"vehicle_id", "discount_cny"},
    "request_quote_approval": {"vehicle_id", "discount_cny"},
    "list_vehicle_reports": {"vehicle_id"},
    "read_vehicle_report": {"vehicle_id", "report_id", "start_chunk", "limit"},
    "finalize_vehicle_report": {"vehicle_id"},
    "search_web": {"time_range"},
}


def configure_trace_logging() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def emit_context_event(event: str, context, *, event_logger=logger, **fields) -> None:
    if context.agent_id:
        fields = {"agent_id": str(context.agent_id), "parent_agent_id": str(context.turn_id), **fields}
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
              "run_id": str(context.run_id), "thread_id": str(context.thread_id),
              "employee_id": str(context.employee_id),
              "skill_id": context.skill.id, "skill_version": context.skill.version, **fields}
    event_logger.info(json.dumps(record, ensure_ascii=False))
    if context.task_id:
        from src.services import tasks

        try:
            tasks.record_event(context.task_id, context.run_id, event, record)
        except Exception as exc:
            event_logger.warning(json.dumps({"event": "event.persistence.error", "run_id": str(context.run_id),
                                            "error_type": type(exc).__name__}))


class AgentTrace(BaseCallbackHandler):
    """为一轮执行记录模型与工具事件，不保存消息正文和原始异常。"""

    run_inline = True

    def __init__(self, run_id: str, thread_id: str, employee_id: str | None = None):
        self.run_id = run_id
        self.thread_id = thread_id
        self.employee_id = employee_id
        self.paused_approval_id = None
        self.skill_id = None
        self.skill_version = None
        self.event_sink = None
        self._spans = {}
        self._lock = Lock()

    def emit(self, event: str, *, level: int = logging.INFO, **fields) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "employee_id": self.employee_id,
            "skill_id": self.skill_id,
            "skill_version": self.skill_version,
            **fields,
        }
        logger.log(level, json.dumps(record, ensure_ascii=False))
        if self.event_sink is not None:
            try:
                self.event_sink(event, record)
            except Exception as exc:
                logger.warning(json.dumps({"event": "event.persistence.error", "run_id": self.run_id,
                                            "error_type": type(exc).__name__}))

    @contextmanager
    def execution(self):
        started = perf_counter()
        self.emit("run.start")
        try:
            yield
        except ExecutionBudgetExceeded as error:
            self.emit("run.limit", level=logging.WARNING,
                      duration_ms=round((perf_counter() - started) * 1000, 2),
                      resource=error.resource, limit=error.limit,
                      used=error.used, requested=error.requested)
            raise
        except Exception as error:
            self.emit("run.error", level=logging.ERROR,
                      duration_ms=round((perf_counter() - started) * 1000, 2),
                      error_type=type(error).__name__)
            raise
        else:
            event = "run.paused" if self.paused_approval_id else "run.end"
            fields = {"approval_id": self.paused_approval_id} if self.paused_approval_id else {}
            self.emit(event, duration_ms=round((perf_counter() - started) * 1000, 2), **fields)

    def _start(self, kind, span_id, parent_span_id, **fields):
        with self._lock:
            scope = {key: fields[key] for key in ("skill_id", "skill_version", "step_id", "agent_id", "parent_agent_id") if key in fields}
            self._spans[span_id] = (perf_counter(), kind, fields.get("tool_name"), scope)
        self.emit(f"{kind}.start", span_id=str(span_id),
                  parent_span_id=str(parent_span_id) if parent_span_id else None, **fields)

    def _end(self, span_id, *, error=None, **fields):
        with self._lock:
            span = self._spans.pop(span_id, None)
        if span is None:
            return
        started, kind, tool_name, scope = span
        fields = {**scope, **fields}
        if tool_name is not None:
            fields["tool_name"] = tool_name
        if error is not None:
            fields["error_type"] = type(error).__name__
        failed = error is not None or fields.get("status") == "error"
        self.emit(f"{kind}.error" if failed else f"{kind}.end",
                  level=logging.WARNING if failed else logging.INFO,
                  span_id=str(span_id),
                  duration_ms=round((perf_counter() - started) * 1000, 2), **fields)

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        metadata = kwargs.get("metadata") or {}
        scope = {key: metadata[key] for key in ("skill_id", "skill_version", "step_id", "agent_id", "parent_agent_id") if key in metadata}
        self._start("model", run_id, parent_run_id, **scope)

    def on_llm_end(self, response, *, run_id, **kwargs):
        messages = [getattr(generation, "message", None)
                    for group in response.generations for generation in group]
        tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        for message in messages:
            usage = getattr(message, "usage_metadata", None) or {}
            for key in tokens:
                tokens[key] += usage.get(key, 0)
        self._end(run_id, tool_call_count=sum(len(getattr(m, "tool_calls", [])) for m in messages),
                  **tokens)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kwargs):
        name = str((serialized or {}).get("name", "unknown"))[:64]
        # 只记录已知工具的允许字段，不序列化整个输入或连接信息。
        arguments = {}
        for key in sorted(TOOL_ARGUMENTS.get(name, set())):
            value = (inputs or {}).get(key)
            if isinstance(value, str):
                arguments[key] = value[:64]
            elif value is None or isinstance(value, (int, bool)):
                if key in (inputs or {}):
                    arguments[key] = value
        metadata = kwargs.get("metadata") or {}
        scope = {key: metadata[key] for key in ("skill_id", "skill_version", "step_id", "agent_id", "parent_agent_id") if key in metadata}
        self._start("tool", run_id, parent_run_id, tool_name=name, arguments=arguments, **scope)

    def on_tool_end(self, output, *, run_id, **kwargs):
        status = getattr(output, "status", "success")
        data = output.content if isinstance(output, ToolMessage) else output
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                data = None
        count = None
        if status == "success" and isinstance(data, dict):
            if isinstance(data.get("vehicles"), list):
                count = len(data["vehicles"])
            elif "vehicle_id" in data:
                count = 1
        fields = {}
        if status == "success" and isinstance(data, dict) and data.get("provider") == "tavily":
            count = len(data.get("sources", []))
            fields = {"provider": "tavily"}
        if status == "success" and isinstance(data, dict) and data.get("provider") == "local_mcp":
            fields = {"provider": "local_mcp", "report_id": data.get("report_id"),
                      "chunk_count": len(data.get("chunks", []))}
        self._end(run_id, status=status, result_count=count, **fields)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error=error)
