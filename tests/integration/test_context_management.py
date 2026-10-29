import json
from unittest.mock import patch
import unittest
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete

from src.agent import vehicle_agent
from src.agent.config import get_settings
from src.api_view.app import app
from src.api_view.auth import get_current_employee
from src.api_view.schemas import EmployeeResponse
from src.database.checkpoints import open_checkpointer
from src.database.connection import SessionFactory, engine
from src.database.models import AgentRunRecord, ConversationRecord, EmployeeRecord, VehicleRecord
from tests.test_conversation_context import historical_messages


class ContextManagementTests(unittest.TestCase):
    def setUp(self):
        self.people, self.threads, self.vehicle_ids = [], [], []
        with SessionFactory.begin() as session:
            for role in ("sales", "manager"):
                record = EmployeeRecord(username=f"context_{uuid4().hex[:20]}", role=role, password_hash="unused")
                session.add(record)
                session.flush()
                self.people.append(EmployeeResponse.model_validate(record))
        self.employee = self.people[0]
        self.addCleanup(self.cleanup_data)
        override = patch.dict(app.dependency_overrides, {get_current_employee: lambda: self.employee})
        override.start()
        self.addCleanup(override.stop)
        settings = get_settings().model_copy(update={"context_window_tokens": 16000,
            "context_output_tokens": 1000, "context_summary_tokens": 4000, "context_recent_turns": 2})
        config_patch = patch.object(vehicle_agent, "get_settings", return_value=settings)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def cleanup_data(self):
        with open_checkpointer() as saver:
            for thread in self.threads:
                saver.delete_thread(thread)
        ids = [person.employee_id for person in self.people]
        with SessionFactory.begin() as session:
            session.execute(delete(AgentRunRecord).where(AgentRunRecord.employee_id.in_(ids)))
            session.execute(delete(ConversationRecord).where(ConversationRecord.employee_id.in_(ids)))
            session.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id.in_(ids)))
            session.execute(delete(VehicleRecord).where(VehicleRecord.vehicle_id.in_(self.vehicle_ids)))

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def seed(self, raw):
        response = self.client.post("/api/conversations", json={"title": "长对话上下文验证"})
        self.assertEqual(response.status_code, 201, response.text)
        thread = response.json()["thread_id"]
        self.threads.append(thread)
        with open_checkpointer(thread) as saver:
            graph = vehicle_agent.build_vehicle_agent(saver)
            graph.update_state({"configurable": {"thread_id": thread}}, {"messages": raw, "plan": None}, as_node="finish")
        return thread

    def test_compressed_reference_queries_live_vehicle_and_full_history_remains_private(self):
        raw = historical_messages()
        vehicle_id = "CTX-" + uuid4().hex[:20]
        self.vehicle_ids.append(vehicle_id)
        old_result = json.loads(raw[2].content)
        old_result["vehicles"][1]["vehicle_id"] = vehicle_id
        raw[2] = raw[2].model_copy(update={"content": json.dumps(old_result)})
        with SessionFactory.begin() as session:
            session.add(VehicleRecord(vehicle_id=vehicle_id, title="上下文验证车辆", price_cny=125000,
                                      mileage_km=60000, is_demo=True))
        thread = self.seed(raw)
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [AIMessage(content="", tool_calls=[{"name": "get_vehicle",
                "args": {"vehicle_id": vehicle_id}, "id": "current-lookup"}]),
                AIMessage(content="模拟车辆当前售价125000元。")]
            response = self.client.post("/api/chat", json={"thread_id": thread, "message": "第二辆现在多少钱？"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["vehicle"]["price_cny"], 125000)
            first_input = model.invoke.call_args_list[0].args[0]
            self.assertIn(vehicle_id, str(first_input))
            self.assertNotIn("90000", str(first_input))
            current_tool = next(message for message in model.invoke.call_args.args[0]
                                if isinstance(message, ToolMessage) and message.tool_call_id == "current-lookup")
            self.assertEqual(json.loads(current_tool.content)["price_cny"], 125000)
        info = self.client.get(f"/api/conversations/{thread}/context")
        self.assertEqual(info.status_code, 200, info.text)
        self.assertGreater(info.json()["digest"]["covered_messages"], 0)
        self.assertLessEqual(info.json()["usage"]["input_estimated_tokens"], info.json()["usage"]["input_limit"])
        history = self.client.get(f"/api/conversations/{thread}").json()
        self.assertEqual(len(history["messages"]), len(raw) + 4)
        self.assertIn("90000", history["messages"][2]["content"])
        self.employee = self.people[1]
        self.assertEqual(self.client.get(f"/api/conversations/{thread}/context").status_code, 404)

    def test_failed_model_call_resumes_persisted_summary_without_new_thread_leakage(self):
        raw = historical_messages()
        thread = self.seed(raw)
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = RuntimeError("test-context-failure")
            failed = self.client.post("/api/chat", json={"thread_id": thread, "message": "继续按预算筛选"})
            self.assertEqual(failed.status_code, 500, failed.text)
            summary = self.client.get(f"/api/conversations/{thread}/context").json()["digest"]
            self.assertGreater(summary["covered_messages"], 0)
            model.invoke.side_effect = None
            model.invoke.return_value = AIMessage(content="已恢复，会重新查询库存。")
            resumed = self.client.post(f"/api/runs/{failed.json()['detail']['run_id']}/resume")
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertIn("十万元", str(model.invoke.call_args.args[0]))
            self.assertEqual(self.client.get(f"/api/conversations/{thread}/context").json()["digest"], summary)
            fresh = self.client.post("/api/chat", json={"message": "你好"})
            self.assertEqual(fresh.status_code, 200, fresh.text)
            self.threads.append(fresh.json()["thread_id"])
            self.assertNotIn("十万元", str(model.invoke.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
