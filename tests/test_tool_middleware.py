from dataclasses import replace
import json
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph

from src.agent.config import get_settings
from src.agent.context import create_context
from src.agent.middleware import ToolExecutionMiddleware
from src.agent.skills import get_skill_registry, TOOL_ROLES
from src.agent.tracing import AgentTrace, logger
from src.domain.execution import ExecutionCancelled, ExecutionTimedOut
from src.services import executions


class ToolMiddlewareTests(unittest.TestCase):
    def setUp(self):
        settings = get_settings().model_copy(update={"max_run_seconds": 30, "tool_timeout_seconds": 5})
        self.context = create_context(uuid4(), "sales", uuid4(), uuid4(), uuid4(),
                                      get_skill_registry().get("inventory_match", "sales"), settings=settings)
        self.invocations = []
        self.cancelled = Event()
        for name, value in (("charge", None), ("cancellation_requested", lambda _: self.cancelled.is_set())):
            patcher = patch.object(executions, name, **({"side_effect": value} if callable(value) else {"return_value": value}))
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            if name == "charge":
                self.charge = mock
        patcher = patch("src.agent.middleware.get_settings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, action, *names, context=None, config=None):
        middleware = ToolExecutionMiddleware([action], context or self.context, self.context.budget)

        def dispatch(state, config: RunnableConfig):
            return middleware.invoke(state, config)

        builder = StateGraph(MessagesState)
        builder.add_node("tools", dispatch)
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        return builder.compile().invoke({"messages": [AIMessage(content="", tool_calls=[{
            "name": name, "args": {"vehicle_id": str(index)}, "id": f"call-{index}", "type": "tool_call",
        } for index, name in enumerate(names)])]}, config={"max_concurrency": 1, **(config or {})})

    def test_mixed_forbidden_batch_is_not_partially_executed_and_rejection_is_persisted(self):
        @tool
        def get_vehicle(vehicle_id: str) -> dict:
            """Return a vehicle for an execution-boundary test."""
            self.invocations.append(vehicle_id)
            return {"vehicle_id": vehicle_id}

        context = replace(self.context, task_id=uuid4())
        with patch("src.services.tasks.record_event") as persist, self.assertLogs(logger, "INFO") as logs:
            result = self.invoke(get_vehicle, "get_vehicle", "create_quote_draft", context=context)
        self.assertEqual(self.invocations, [])
        self.charge.assert_not_called()
        self.assertTrue(all(message.status == "error" for message in result["messages"][1:]))
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual(record["error_code"], "tool_permission_denied")
        self.assertNotIn("arguments", record)
        self.assertEqual(persist.call_args.args[:3], (context.task_id, context.run_id, "tool.batch.rejected"))

    def test_current_role_policy_is_rechecked_after_skill_snapshot_creation(self):
        @tool
        def get_vehicle(vehicle_id: str) -> dict:
            """Return a vehicle for an execution-boundary test."""
            self.invocations.append(vehicle_id)
            return {"vehicle_id": vehicle_id}

        with patch.dict(TOOL_ROLES, {"get_vehicle": frozenset({"manager"})}), self.assertLogs(logger, "INFO"):
            result = self.invoke(get_vehicle, "get_vehicle")
        self.assertEqual(result["messages"][-1].status, "error")
        self.assertEqual(self.invocations, [])
        self.charge.assert_not_called()

    def test_cancellation_between_serial_calls_prevents_second_tool_and_propagates(self):
        @tool
        def get_vehicle(vehicle_id: str) -> dict:
            """Cancel after the first tool side effect."""
            self.invocations.append(vehicle_id)
            self.cancelled.set()
            return {"vehicle_id": vehicle_id}

        with self.assertLogs(logger, "INFO"), self.assertRaises(ExecutionCancelled):
            self.invoke(get_vehicle, "get_vehicle", "get_vehicle")
        self.assertEqual(self.invocations, ["0"])
        self.assertEqual(self.charge.call_args.args[2:4], ("tool_calls", 2))

    def test_control_signals_raised_inside_tools_are_not_converted_to_model_feedback(self):
        for signal in (ExecutionCancelled, ExecutionTimedOut):
            with self.subTest(signal=signal.__name__):
                @tool
                def get_vehicle(vehicle_id: str) -> dict:
                    """Raise a runtime control signal from a tool."""
                    raise signal("execution stopped")

                with self.assertLogs(logger, "INFO"), self.assertRaises(signal):
                    self.invoke(get_vehicle, "get_vehicle")

    def test_each_tool_receives_fresh_deadline_and_server_identity_without_duplicate_trace_events(self):
        configurations = []

        @tool
        def get_vehicle(vehicle_id: str, config: RunnableConfig) -> dict:
            """Capture the configuration actually delivered to each tool."""
            configurations.append(dict(config["configurable"]))
            if len(configurations) == 1:
                self.context.control.deadline = monotonic() + 2
            return {"vehicle_id": vehicle_id}

        trace = AgentTrace(str(self.context.run_id), str(self.context.thread_id), str(self.context.employee_id))
        original = {"employee_id": "untrusted", "tool_timeout_ms": 999999}
        with self.assertLogs(logger, "INFO") as logs:
            self.invoke(get_vehicle, "get_vehicle", "get_vehicle", config={"configurable": original, "callbacks": [trace]})
        self.assertEqual(len(configurations), 2)
        self.assertEqual(configurations[0]["tool_timeout_ms"], 5000)
        self.assertLessEqual(configurations[1]["tool_timeout_ms"], 2000)
        self.assertLessEqual(configurations[1]["mcp_timeout_ms"], 2000)
        self.assertTrue(all(item["employee_id"] == str(self.context.employee_id) for item in configurations))
        self.assertTrue(all(item["run_id"] == str(self.context.run_id) for item in configurations))
        self.assertEqual(original, {"employee_id": "untrusted", "tool_timeout_ms": 999999})
        self.assertEqual([json.loads(item.getMessage())["event"] for item in logs.records],
                         ["tool.start", "tool.end", "tool.start", "tool.end"])


if __name__ == "__main__":
    unittest.main()
