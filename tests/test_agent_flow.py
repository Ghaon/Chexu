import importlib
from contextlib import nullcontext
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.exc import OperationalError
from tests.support import install_test_identity


# 使用独立配置和模型替身，检查期间不使用真实密钥或发出模型请求。
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
    middleware_module = importlib.import_module("src.agent.middleware")


class AgentFlowTests(unittest.TestCase):
    def setUp(self):
        install_test_identity(self)
        memory = InMemorySaver()
        memory_patch = patch(
            "src.agent.runtime.open_checkpointer",
            side_effect=lambda thread_id: nullcontext(memory),
        )
        memory_patch.start()
        self.addCleanup(memory_patch.stop)
        self.client = TestClient(api_module.app)
        self.addCleanup(self.client.close)
        factory_patch = patch.object(tool_module, "SessionFactory")
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.session = self.factory.return_value.__enter__.return_value
        self.session.scalar.return_value = None

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "live")

    def test_direct_answer_has_no_vehicle(self):
        with patch.object(graph_module, "tool_model") as model:
            model.invoke.return_value = AIMessage(content="请提供车辆编号。")
            response = self.client.post("/api/chat", json={"message": "查一下车辆"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["vehicle"])
        self.assertEqual(model.invoke.call_count, 1)

    def test_tool_result_is_sent_back_to_model(self):
        self.session.scalar.return_value = tool_module.VehicleRecord(
            vehicle_id="DEMO-001", title="工具流程检查车辆",
            price_cny=96800, mileage_km=64000, is_demo=True,
        )
        with patch.object(graph_module, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="", tool_calls=[{
                    "name": "get_vehicle",
                    "args": {"vehicle_id": "DEMO-001"},
                    "id": "lookup-1",
                    "type": "tool_call",
                }]),
                AIMessage(content="模拟车辆售价为 96800 元，里程为 64000 公里。"),
            ]
            response = self.client.post(
                "/api/chat", json={"message": "DEMO-001 多少钱，多少公里？"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["vehicle"]["vehicle_id"], "DEMO-001")
        self.assertEqual(response.json()["vehicle"]["price_cny"], 96800)
        self.assertEqual(model.invoke.call_count, 2)
        messages = model.invoke.call_args_list[1].args[0]
        result = next(message for message in messages if isinstance(message, ToolMessage))
        self.assertEqual(result.tool_call_id, "lookup-1")
        self.assertEqual(result.status, "success")

    def test_missing_vehicle_is_returned_as_tool_error(self):
        with patch.object(graph_module, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="", tool_calls=[{
                    "name": "get_vehicle",
                    "args": {"vehicle_id": "MISSING"},
                    "id": "lookup-missing",
                    "type": "tool_call",
                }]),
                AIMessage(content="车辆查询失败，请核对编号。"),
            ]
            response = self.client.post("/api/chat", json={"message": "查询 MISSING"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["vehicle"])
        messages = model.invoke.call_args_list[1].args[0]
        self.assertEqual(messages[-1].status, "error")
        self.assertIn("未找到", messages[-1].content)

    def test_failed_latest_lookup_does_not_show_old_vehicle(self):
        result = graph_module.finish({"messages": [
            ToolMessage(
                content=json.dumps({"vehicle_id": "DEMO-001"}),
                name="get_vehicle", tool_call_id="first", status="success",
            ),
            ToolMessage(
                content="查询失败", name="get_vehicle",
                tool_call_id="second", status="error",
            ),
            AIMessage(content="第二辆车查询失败。"),
        ]})
        self.assertIsNone(result["vehicle"])

    def test_requests_do_not_share_messages(self):
        with patch.object(graph_module, "tool_model") as model:
            model.invoke.return_value = AIMessage(content="请提供车辆编号。")
            self.client.post("/api/chat", json={"message": "查询 DEMO-001"})
            self.client.post("/api/chat", json={"message": "它多少钱？"})
        messages = model.invoke.call_args_list[1].args[0]
        human_messages = [m.content for m in messages if isinstance(m, HumanMessage)]
        self.assertEqual(human_messages, ["它多少钱？"])

    def test_whitespace_input_is_rejected_without_model_call(self):
        with patch.object(graph_module, "tool_model") as model:
            response = self.client.post("/api/chat", json={"message": "  \n "})
        self.assertEqual(response.status_code, 422)
        model.invoke.assert_not_called()

    def test_graph_limit_has_safe_error_response(self):
        with patch.object(api_module, "run_chat", side_effect=GraphRecursionError("limit")):
            with self.assertLogs(api_module.logger, level="WARNING"):
                response = self.client.post("/api/chat", json={"message": "查车"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("limit", response.text)

    def test_unexpected_error_is_logged_as_server_error(self):
        with patch.object(api_module, "run_chat", side_effect=RuntimeError("private-detail")):
            with self.assertLogs(api_module.logger, level="ERROR"):
                response = self.client.post("/api/chat", json={"message": "查车"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-detail", response.text)

    def test_database_error_is_sanitized_and_session_is_closed(self):
        self.session.scalar.side_effect = OperationalError(
            None, None, RuntimeError("private-db-detail")
        )
        with self.assertLogs(tool_module.logger, level="WARNING") as logs:
            with self.assertRaisesRegex(tool_module.VehicleToolError, "暂时不可用"):
                tool_module.get_vehicle.invoke({"vehicle_id": "DEMO-001"})
        self.assertNotIn("private-db-detail", " ".join(logs.output))
        self.factory.return_value.__exit__.assert_called_once()

    def test_invalid_vehicle_id_does_not_open_session(self):
        for vehicle_id in ("   ", "X" * 33, "X\x00Y"):
            with self.subTest(vehicle_id=vehicle_id):
                with self.assertRaisesRegex(tool_module.VehicleToolError, "有效的车辆编号"):
                    tool_module.get_vehicle.invoke({"vehicle_id": vehicle_id})
        self.factory.assert_not_called()

    def test_unexpected_tool_failure_does_not_leak_details_to_model(self):
        self.session.scalar.side_effect = RuntimeError("private-tool-detail")
        with patch.object(graph_module, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="", tool_calls=[{
                    "name": "get_vehicle", "args": {"vehicle_id": "CX-0001"},
                    "id": "failed-lookup", "type": "tool_call",
                }]),
                AIMessage(content="车辆查询失败，请稍后重试。"),
            ]
            with self.assertLogs(middleware_module.logger, level="ERROR") as logs:
                response = self.client.post("/api/chat", json={"message": "查 CX-0001"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["vehicle"])
        tool_result = model.invoke.call_args_list[1].args[0][-1]
        self.assertEqual(tool_result.status, "error")
        self.assertNotIn("private-tool-detail", tool_result.content)
        self.assertNotIn("private-tool-detail", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
