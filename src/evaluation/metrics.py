from collections import Counter
import json
import logging
from math import ceil
from threading import Lock

from langchain_core.callbacks import BaseCallbackHandler


class EvaluationBudgetExceeded(Exception):
    pass


class ModelBudget(BaseCallbackHandler):
    """在真实请求前限制总调用次数；未返回 usage 的请求按未知用量处理。"""
    raise_error = True
    run_inline = True

    def __init__(self, limit: int):
        self.limit, self.calls, self.completed, self.failed, self.reported = limit, 0, 0, 0, 0
        self.input_tokens = self.output_tokens = 0
        self.active = set()
        self.planner_runs, self.planning_outputs = set(), []
        self.lock = Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        with self.lock:
            if self.calls >= self.limit:
                raise EvaluationBudgetExceeded("本次评估的模型调用预算已用尽。")
            self.calls += 1
            self.active.add(run_id)
            if any(item.get("function", {}).get("name") == "submit_task_plan"
                   for item in (kwargs.get("invocation_params") or {}).get("tools", [])):
                self.planner_runs.add(run_id)

    def on_llm_end(self, response, *, run_id, **kwargs):
        with self.lock:
            if run_id not in self.active:
                return
            self.active.remove(run_id)
            self.completed += 1
            messages = [getattr(item, "message", None) for group in response.generations for item in group]
            if run_id in self.planner_runs:
                self.planner_runs.remove(run_id)
                for message in messages:
                    self.planning_outputs.append({"text": str(getattr(message, "content", ""))[:4000],
                                                  "tool_calls": getattr(message, "tool_calls", [])})
            usages = [getattr(item, "usage_metadata", None) for item in messages]
            if messages and all(usage is not None for usage in usages):
                self.reported += 1
                self.input_tokens += sum(usage.get("input_tokens", 0) for usage in usages)
                self.output_tokens += sum(usage.get("output_tokens", 0) for usage in usages)

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self.lock:
            if run_id in self.active:
                self.active.remove(run_id)
                self.failed += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {"model_calls": self.calls, "completed_model_calls": self.completed,
                    "failed_model_calls": self.failed, "usage_reported_calls": self.reported,
                    "known_input_tokens": self.input_tokens, "known_output_tokens": self.output_tokens}


class TraceCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        try:
            event = json.loads(record.getMessage())
            if len(self.events) < 4000:
                self.events.append(event)
        except (ValueError, TypeError):
            pass


def delta(after: dict, before: dict) -> dict:
    return {key: value - before.get(key, 0) for key, value in after.items()}


def summarize(results: list[dict], input_price: float | None = None, output_price: float | None = None) -> dict:
    statuses = Counter(item["status"] for item in results)
    executed = [item for item in results if item["status"] != "not_run_budget"]
    durations = sorted(item["duration_seconds"] for item in executed)
    metrics = {key: sum(item.get("metrics", {}).get(key, 0) for item in results)
               for key in ("model_calls", "completed_model_calls", "failed_model_calls", "usage_reported_calls",
                           "known_input_tokens", "known_output_tokens")}
    complete_usage = metrics["model_calls"] > 0 and metrics["usage_reported_calls"] == metrics["model_calls"]
    cost = None
    if complete_usage and input_price is not None and output_price is not None:
        cost = round((metrics["known_input_tokens"] * input_price + metrics["known_output_tokens"] * output_price) / 1_000_000, 6)
    return {"selected": len(results), "executed": len(executed), "passed": statuses["passed"],
            "failed": statuses["failed"], "errors": statuses["error"], "not_run_budget": statuses["not_run_budget"],
            "success_rate_executed": statuses["passed"] / len(executed) if executed else None,
            "completion_rate_selected": statuses["passed"] / len(results) if results else None,
            "p50_seconds": durations[ceil(len(durations) * 0.5) - 1] if durations else None,
            "p95_seconds": durations[ceil(len(durations) * 0.95) - 1] if durations else None,
            **metrics, "usage_complete": complete_usage, "estimated_cost_usd": cost,
            "cost_note": "按提供的每百万 token 单价估算" if cost is not None else "未提供单价或 usage 不完整，费用未知"}
