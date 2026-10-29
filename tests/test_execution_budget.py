from contextlib import nullcontext
import importlib
import json
import os
import unittest
from unittest.mock import patch

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
    runtime_module = importlib.import_module("src.agent.runtime")
    api_module = importlib.import_module("src.api_view.app")
    tool_module = importlib.import_module("src.agent.tools.vehicle_tools")
    tracing = importlib.import_module("src.agent.tracing")


class ExecutionBudgetTests(unittest.TestCase):
    def setUp(self):
        install_test_identity(self)
        self.memory = InMemorySaver()
        memory_patch = patch.object(runtime_module, "open_checkpointer",
                                    side_effect=lambda thread_id: nullcontext(self.memory))
        memory_patch.start()
        self.addCleanup(memory_patch.stop)
        self.settings = runtime_module.get_settings().model_copy(update={
            "max_model_calls": 2, "max_tool_calls": 1,
        })
        settings_patch = patch.object(runtime_module, "get_settings", return_value=self.settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        factory_patch = patch.object(tool_module, "SessionFactory")
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.session = self.factory.return_value.__enter__.return_value
        self.session.scalar.return_value = tool_module.VehicleRecord(
            vehicle_id="BUDGET-001", title="预算检查车辆",
            price_cny=86000, mileage_km=52000, is_demo=True,
        )
        self.client = TestClient(api_module.app)
        self.addCleanup(self.client.close)

    def tool_request(self, *call_ids):
        return AIMessage(content="", tool_calls=[{
            "name": "get_vehicle", "args": {"vehicle_id": "BUDGET-001"},
            "id": call_id, "type": "tool_call",
        } for call_id in call_ids])

    def records(self, captured):
        return [json.loads(record.getMessage()) for record in captured.records]

    def assert_limit(self, response, captured, resource, limit, used, requested):
        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "execution_budget_exceeded")
        self.assertEqual((detail["resource"], detail["limit"], detail["used"], detail["requested"]),
                         (resource, limit, used, requested))
        records = self.records(captured)
        self.assertEqual(records[-1]["event"], "run.limit")
        self.assertEqual(records[-1]["run_id"], detail["run_id"])
        self.assertEqual(records[-1]["resource"], resource)
        self.assertNotIn("run.error", [record["event"] for record in records])

    def test_model_limit_blocks_next_model_call_before_it_starts(self):
        self.settings.max_model_calls = 1
        model = FakeMessagesListChatModel(responses=[self.tool_request("first"), AIMessage(content="不应生成")])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            response = self.client.post("/api/chat", json={"message": "查 BUDGET-001"})
        self.assert_limit(response, logs, "model_calls", 1, 1, 1)
        self.assertEqual(sum(r["event"] == "model.start" for r in self.records(logs)), 1)
        self.session.scalar.assert_called_once()

    def test_parallel_tool_batch_is_rejected_before_any_tool_runs(self):
        model = FakeMessagesListChatModel(responses=[self.tool_request("first", "second")])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            response = self.client.post("/api/chat", json={"message": "查询两次车辆"})
        self.assert_limit(response, logs, "tool_calls", 1, 0, 2)
        self.factory.assert_not_called()
        self.assertFalse(any(r["event"] == "tool.start" for r in self.records(logs)))

    def test_failed_tool_attempt_still_consumes_budget(self):
        self.session.scalar.return_value = None
        model = FakeMessagesListChatModel(responses=[self.tool_request("first"), self.tool_request("retry")])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            response = self.client.post("/api/chat", json={"message": "查询不存在的车"})
        self.assert_limit(response, logs, "tool_calls", 1, 1, 1)
        self.session.scalar.assert_called_once()
        self.assertTrue(any(r["event"] == "tool.error" for r in self.records(logs)))

    def test_exact_budget_succeeds_and_next_turn_gets_fresh_budget(self):
        model = FakeMessagesListChatModel(responses=[
            self.tool_request("first"), AIMessage(content="车辆售价 86000 元。"),
            self.tool_request("second"), AIMessage(content="重新查询后，售价仍为 86000 元。"),
        ])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            first = self.client.post("/api/chat", json={"message": "查询 BUDGET-001"})
            self.assertEqual(first.status_code, 200, first.text)
            second = self.client.post("/api/chat", json={
                "thread_id": first.json()["thread_id"], "message": "它现在多少钱？",
            })
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["vehicle"]["price_cny"], 86000)
        self.assertEqual(self.session.scalar.call_count, 2)
        records = self.records(logs)
        self.assertEqual(sum(r["event"] == "run.end" for r in records), 2)
        self.assertEqual(sum(r["event"] == "model.start" for r in records), 4)
        self.assertFalse(any(r["event"] == "run.limit" for r in records))


if __name__ == "__main__":
    unittest.main()
