from contextlib import nullcontext
import importlib
import json
import os
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from tests.support import install_test_identity


with patch.dict(os.environ, {
    "CHEXU_MODE": "live",
    "CHEXU_MODEL_BASE_URL": "http://127.0.0.1:1/v1",
    "CHEXU_MODEL_NAME": "test-model",
    "CHEXU_MODEL_API_KEY": "test-key",
    "CHEXU_DATABASE_URL": "postgresql+psycopg://test:test@127.0.0.1:1/test",
}):
    graph_module = importlib.import_module("src.agent.vehicle_agent")
    api_module = importlib.import_module("src.api_view.app")
    tool_module = importlib.import_module("src.agent.tools.vehicle_tools")
    tracing = importlib.import_module("src.agent.tracing")


class FailingChatModel(FakeMessagesListChatModel):
    def _generate(self, *args, **kwargs):
        raise RuntimeError("TRACE-PRIVATE-ERROR")


class AgentTracingTests(unittest.TestCase):
    def setUp(self):
        install_test_identity(self)
        memory = InMemorySaver()
        memory_patch = patch("src.agent.runtime.open_checkpointer",
                             side_effect=lambda thread_id: nullcontext(memory))
        memory_patch.start()
        self.addCleanup(memory_patch.stop)
        self.client = TestClient(api_module.app)
        self.addCleanup(self.client.close)
        factory_patch = patch.object(tool_module, "SessionFactory")
        factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.session = factory.return_value.__enter__.return_value
        self.session.scalar.return_value = tool_module.VehicleRecord(
            vehicle_id="TRACE-001", title="TRACE-PRIVATE-VEHICLE",
            price_cny=86000, mileage_km=52000, is_demo=True,
        )

    def tool_request(self):
        return AIMessage(content="", tool_calls=[{
            "name": "get_vehicle", "args": {"vehicle_id": "TRACE-001"},
            "id": "trace-lookup", "type": "tool_call",
        }], usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    def records(self, captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    def test_success_links_model_and_tool_events_without_logging_message_bodies(self):
        model = FakeMessagesListChatModel(responses=[
            self.tool_request(), AIMessage(content="TRACE-PRIVATE-ANSWER"),
        ])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            response = self.client.post("/api/chat", json={"message": "TRACE-PRIVATE-USER"})
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run_id"]
        UUID(run_id)
        records = self.records(logs)
        self.assertEqual([r["event"] for r in records], [
            "run.start", "model.start", "model.end", "tool.start", "tool.end",
            "model.start", "model.end", "run.end",
        ])
        self.assertTrue(all(r["run_id"] == run_id for r in records))
        self.assertTrue(all(r["thread_id"] == response.json()["thread_id"] for r in records))
        started = next(r for r in records if r["event"] == "tool.start")
        self.assertEqual(started["arguments"], {"vehicle_id": "TRACE-001"})
        ended = next(r for r in records if r["event"] == "tool.end")
        self.assertEqual(started["span_id"], ended["span_id"])
        self.assertEqual(ended["result_count"], 1)
        self.assertEqual(ended["status"], "success")
        self.assertEqual(records[2]["total_tokens"], 15)
        self.assertTrue(all(r["duration_ms"] >= 0 for r in records if "duration_ms" in r))
        self.assertNotIn("TRACE-PRIVATE", " ".join(logs.output))

    def test_handled_tool_failure_is_visible_even_when_chat_completes(self):
        self.session.scalar.side_effect = tool_module.VehicleToolError("TRACE-PRIVATE-TOOL-ERROR")
        model = FakeMessagesListChatModel(responses=[self.tool_request(), AIMessage(content="查询失败。")])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            response = self.client.post("/api/chat", json={"message": "查车"})
        self.assertEqual(response.status_code, 200, response.text)
        records = self.records(logs)
        failure = next(r for r in records if r["event"] == "tool.error")
        self.assertEqual(failure["error_type"], "VehicleToolError")
        self.assertEqual(records[-1]["event"], "run.end")
        self.assertNotIn("TRACE-PRIVATE", " ".join(logs.output))

    def test_model_failure_returns_trace_id_and_logs_only_error_type(self):
        model = FailingChatModel(responses=[AIMessage(content="unused")])
        with patch.object(graph_module, "tool_model", model), \
             self.assertLogs(tracing.logger, "INFO") as logs, \
             self.assertLogs(api_module.logger, "ERROR"):
            response = self.client.post("/api/chat", json={"message": "查车"})
        self.assertEqual(response.status_code, 500, response.text)
        run_id = response.json()["detail"]["run_id"]
        UUID(run_id)
        records = self.records(logs)
        self.assertEqual([r["event"] for r in records], [
            "run.start", "model.start", "model.error", "run.error",
        ])
        self.assertTrue(all(r["run_id"] == run_id for r in records))
        self.assertEqual(records[-1]["error_type"], "RuntimeError")
        self.assertNotIn("TRACE-PRIVATE", " ".join(logs.output) + response.text)

    def test_follow_up_keeps_thread_id_but_gets_a_new_run_id(self):
        model = FakeMessagesListChatModel(responses=[AIMessage(content="请提供车辆编号。")])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            first = self.client.post("/api/chat", json={"message": "查车"})
            self.assertEqual(first.status_code, 200, first.text)
            second = self.client.post("/api/chat", json={
                "thread_id": first.json()["thread_id"], "message": "继续",
            })
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["thread_id"], second.json()["thread_id"])
        self.assertNotEqual(first.json()["run_id"], second.json()["run_id"])
        starts = [r["run_id"] for r in self.records(logs) if r["event"] == "run.start"]
        self.assertEqual(starts, [first.json()["run_id"], second.json()["run_id"]])


if __name__ == "__main__":
    unittest.main()
