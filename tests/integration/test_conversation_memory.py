import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.agent import vehicle_agent as graph_module
from src.agent.config import Settings
from src.agent.tools import vehicle_tools
from src.api_view import app as api_module
from src.database import checkpoints
from tests.support import install_test_identity


class ConversationMemoryTests(unittest.TestCase):
    def setUp(self):
        install_test_identity(self)
        settings_patch = patch.object(checkpoints, "get_settings", return_value=Settings())
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.thread_ids = set()
        self.addCleanup(self.delete_test_threads)

    def delete_test_threads(self):
        # 仅清理本次生成的会话，不接触车辆数据或其他会话。
        with checkpoints.open_checkpointer() as saver:
            for thread_id in self.thread_ids:
                saver.delete_thread(thread_id)

    def new_thread_id(self):
        thread_id = str(uuid4())
        self.thread_ids.add(thread_id)
        return thread_id

    def test_history_survives_rebuilt_graph_and_is_isolated_between_threads(self):
        with patch.object(graph_module, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="记下了，你的预算是十万元。"),
                AIMessage(content="你的预算是十万元。"),
                AIMessage(content="请先告诉我预算。"),
            ]
            with TestClient(api_module.app) as client:
                first = client.post("/api/chat", json={"message": "我的预算是十万元。"})
            self.assertEqual(first.status_code, 200, first.text)
            thread_id = first.json()["thread_id"]
            self.thread_ids.add(thread_id)
            UUID(thread_id)

            # 每次请求都会重建图和数据库连接，历史必须从 PostgreSQL 恢复。
            with TestClient(api_module.app) as client:
                second = client.post("/api/chat", json={
                    "thread_id": thread_id, "message": "我的预算是多少？",
                })
                other_id = self.new_thread_id()
                third = client.post("/api/chat", json={
                    "thread_id": other_id, "message": "我的预算是多少？",
                })
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["thread_id"], thread_id)
        self.assertEqual(third.status_code, 200, third.text)
        second_history = model.invoke.call_args_list[1].args[0]
        third_history = model.invoke.call_args_list[2].args[0]
        self.assertEqual([m.content for m in second_history if isinstance(m, HumanMessage)], [
            "我的预算是十万元。", "我的预算是多少？",
        ])
        self.assertEqual([m.content for m in third_history if isinstance(m, HumanMessage)], [
            "我的预算是多少？",
        ])

    def test_tool_history_is_restored_without_reusing_old_vehicle_card(self):
        thread_id = self.new_thread_id()
        with patch.object(vehicle_tools, "SessionFactory") as factory:
            factory.return_value.__enter__.return_value.scalar.return_value = vehicle_tools.VehicleRecord(
                vehicle_id="MEMORY-TEST", title="会话检查车辆",
                price_cny=96800, mileage_km=64000, is_demo=True,
            )
            with TestClient(api_module.app) as client, patch.object(graph_module, "tool_model") as model:
                model.invoke.side_effect = [
                    AIMessage(content="", tool_calls=[{
                        "name": "get_vehicle", "args": {"vehicle_id": "MEMORY-TEST"},
                        "id": "memory-lookup", "type": "tool_call",
                    }]),
                    AIMessage(content="模拟车辆售价 96800 元。"),
                    AIMessage(content="不客气。"),
                ]
                first = client.post("/api/chat", json={
                    "thread_id": thread_id, "message": "查询 MEMORY-TEST",
                })
                second = client.post("/api/chat", json={
                    "thread_id": thread_id, "message": "谢谢。",
                })
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["vehicle"]["vehicle_id"], "MEMORY-TEST")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertIsNone(second.json()["vehicle"])
        self.assertEqual(second.json()["vehicles"], [])
        self.assertTrue(any(isinstance(m, ToolMessage) for m in model.invoke.call_args_list[2].args[0]))

    def test_same_thread_is_busy_until_previous_connection_closes(self):
        thread_id = self.new_thread_id()
        with TestClient(api_module.app) as client, patch.object(graph_module, "tool_model") as model:
            model.invoke.return_value = AIMessage(content="请提供车辆编号。")
            with checkpoints.open_checkpointer(thread_id):
                busy = client.post("/api/chat", json={"thread_id": thread_id, "message": "查车"})
            self.assertEqual(busy.status_code, 409, busy.text)
            model.invoke.assert_not_called()
            following = client.post("/api/chat", json={"thread_id": thread_id, "message": "查车"})
        self.assertEqual(following.status_code, 200, following.text)

    def test_failed_turn_is_not_silently_combined_with_new_input(self):
        thread_id = self.new_thread_id()
        with TestClient(api_module.app) as client, patch.object(graph_module, "tool_model") as model:
            model.invoke.side_effect = RuntimeError("private-model-detail")
            with self.assertLogs(api_module.logger, level="ERROR"):
                failed = client.post("/api/chat", json={"thread_id": thread_id, "message": "查车"})
            self.assertEqual(failed.status_code, 500)
            self.assertNotIn("private-model-detail", failed.text)
            self.assertEqual(failed.json()["detail"]["thread_id"], thread_id)
            model.reset_mock()
            retry = client.post("/api/chat", json={"thread_id": thread_id, "message": "再试一次"})
            self.assertEqual(retry.status_code, 409, retry.text)
            invalid = client.post("/api/chat", json={"thread_id": "invalid", "message": "查车"})
            self.assertEqual(invalid.status_code, 422)
            model.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
