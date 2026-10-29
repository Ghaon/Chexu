import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import delete

from src.agent import vehicle_agent
from src.agent.skills import get_skill_registry
from src.agent.tools.memory_tools import propose_user_memory
from src.api_view.app import app
from src.api_view.auth import get_current_employee
from src.api_view.schemas import EmployeeResponse
from src.database.checkpoints import open_checkpointer
from src.database.connection import SessionFactory, engine
from src.database.models import AgentRunRecord, ConversationRecord, EmployeeRecord
from src.services import memories


class LongTermMemoryTests(unittest.TestCase):
    def setUp(self):
        self.people, self.threads = [], []
        with SessionFactory.begin() as session:
            for role in ("sales", "manager"):
                record = EmployeeRecord(username=f"memory_{uuid4().hex[:20]}", role=role, password_hash="unused")
                session.add(record)
                session.flush()
                self.people.append(EmployeeResponse.model_validate(record))
        self.employee = self.people[0]
        self.addCleanup(self.cleanup_data)
        override = patch.dict(app.dependency_overrides, {get_current_employee: lambda: self.employee})
        override.start()
        self.addCleanup(override.stop)
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

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def chat(self, message, replies, **extra):
        thread = str(uuid4())
        self.threads.append(thread)
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = replies
            response = self.client.post("/api/chat", json={"thread_id": thread, "message": message, **extra})
            self.assertEqual(response.status_code, 200, response.text)
            prompts = [call.args[0][0].content for call in model.invoke.call_args_list]
        return response.json(), prompts

    def propose(self, category="budget", value=100000, message="请记住我的预算十万元"):
        result, _ = self.chat(message, [
            AIMessage(content="", tool_calls=[{"id": "remember", "name": "propose_user_memory",
                "args": {"category": category, "value": value, "source_quote": message}}]),
            AIMessage(content="已经永久记住了"),
        ], skill_id="user_memory")
        self.assertIn("待确认", result["reply"])
        self.assertEqual(len(result["memory_proposals"]), 1)
        return result["memory_proposals"][0]

    def test_proposal_confirm_edit_delete_and_live_cross_conversation_context(self):
        candidate = self.propose()
        self.assertEqual(candidate["evidence_level"], "unconfirmed_candidate")
        self.assertEqual(memories.confirmed_context(self.employee.employee_id), "")
        path = "/api/memories/" + candidate["memory_id"]
        self.assertEqual(self.client.post(path + "/confirm", json={"confirm": False, "expected_revision": 1}).status_code, 422)
        response = self.client.post(path + "/confirm", json={"confirm": True, "expected_revision": 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["evidence_level"], "user_confirmed")
        _, prompts = self.chat("按我的预算找车", [AIMessage(content="正在根据预算筛选")])
        self.assertIn('"budget": 100000', prompts[0])
        updated = self.client.patch(path, json={"confirm": True, "expected_revision": 2, "value": 120000})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(self.client.patch(path, json={"confirm": True, "expected_revision": 2, "value": 90000}).status_code, 409)
        _, prompts = self.chat("按预算筛选", [AIMessage(content="新预算已读取")])
        self.assertIn('"budget": 120000', prompts[0])
        self.assertNotIn('"budget": 100000', prompts[0])
        self.assertEqual(self.client.delete(path + "?expected_revision=2").status_code, 409)
        self.assertEqual(self.client.delete(path + "?expected_revision=3").status_code, 204)
        _, prompts = self.chat("查找库存", [AIMessage(content="请提供筛选条件")])
        self.assertNotIn("用户已确认的长期记忆", prompts[0])

    def test_crud_validation_and_owner_boundary_including_manager(self):
        for category, value in (("budget", 150000), ("usage", "日常通勤"), ("preferences", ["省油", "自动挡"])):
            response = self.client.post("/api/memories", json={"category": category, "value": value, "confirm": True})
            self.assertEqual(response.status_code, 201, response.text)
        items = self.client.get("/api/memories?status=confirmed").json()
        self.assertEqual(len(items), 3)
        item = next(item for item in items if item["category"] == "budget")
        self.employee = self.people[1]
        path = "/api/memories/" + item["memory_id"]
        self.assertEqual(self.client.get("/api/memories").json(), [])
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.patch(path, json={"confirm": True, "expected_revision": 1, "value": 1}).status_code, 404)
        self.assertEqual(self.client.post(path + "/confirm", json={"confirm": True, "expected_revision": 1}).status_code, 404)
        self.assertEqual(self.client.delete(path + "?expected_revision=1").status_code, 404)
        self.assertEqual(memories.confirmed_context(self.employee.employee_id), "")
        self.employee = self.people[0]
        for category, value in (("budget", True), ("budget", "100000"), ("usage", []), ("preferences", "省油")):
            with self.subTest(category=category, value=value):
                self.assertEqual(self.client.post("/api/memories", json={"category": category, "value": value, "confirm": True}).status_code, 422)
        self.assertEqual(self.client.post("/api/memories", json={"category": "budget", "value": 1}).status_code, 422)

    def test_pending_candidate_is_not_injected_and_replacement_has_one_confirmed_category(self):
        self.client.post("/api/memories", json={"category": "usage", "value": "日常通勤", "confirm": True})
        candidate = self.propose("usage", "临时推测只越野", "请记录用途：临时推测只越野")
        _, prompts = self.chat("我的用途是什么", [AIMessage(content="日常通勤")], skill_id="user_memory")
        self.assertIn("日常通勤", prompts[0])
        self.assertNotIn("临时推测只越野", prompts[0])
        response = self.client.post(f"/api/memories/{candidate['memory_id']}/confirm", json={"confirm": True, "expected_revision": 1})
        self.assertEqual(response.status_code, 200, response.text)
        items = self.client.get("/api/memories?status=confirmed").json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["value"], "临时推测只越野")

    def test_tool_requires_opt_in_and_exposes_no_identity_or_confirmation_arguments(self):
        parameters = convert_to_openai_tool(propose_user_memory)["function"]["parameters"]["properties"]
        self.assertEqual(set(parameters), {"category", "value", "source_quote"})
        registry = get_skill_registry()
        self.assertEqual(registry.select("请记住我的预算十万", "sales").id, "user_memory")
        for message, quote in (("不要记住我的预算十万", "预算十万"), ("我可能喜欢SUV", "喜欢SUV"),
                               ("记住预算十万", "并不存在的原话")):
            with self.subTest(message=message):
                result, _ = self.chat(message, [
                    AIMessage(content="", tool_calls=[{"id": "denied", "name": "propose_user_memory",
                        "args": {"category": "budget", "value": 100000, "source_quote": quote}}]),
                    AIMessage(content="没有保存记忆"),
                ], skill_id="user_memory")
                self.assertEqual(result["memory_proposals"], [])
        self.assertEqual(self.client.get("/api/memories").json(), [])


if __name__ == "__main__":
    unittest.main()
