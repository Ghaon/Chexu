from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError

from src.agent import runtime, vehicle_agent
from src.agent.config import get_settings
from src.agent.tools import vehicle_tools
from src.api_view.app import app
from src.database.checkpoints import open_checkpointer
from src.database.connection import SessionFactory, engine
from src.database.models import AgentRunRecord, ConversationRecord, EmployeeRecord, LoginAttemptRecord
from src.database.timeouts import apply_query_timeout
from src.security import hash_password


class RunControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "run-control-password-2026"
        cls.password_hash = hash_password(cls.password)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        self.people = {}
        self.threads = set()
        with SessionFactory.begin() as session:
            for key, role in (("owner", "sales"), ("other", "manager")):
                person = EmployeeRecord(username=f"run_{uuid4().hex[:20]}", role=role, password_hash=self.password_hash)
                session.add(person)
                session.flush()
                self.people[key] = (person.employee_id, person.username)
        self.addCleanup(self.cleanup_data)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {}
        for key, (_, username) in self.people.items():
            response = self.client.post("/api/auth/login", json={"username": username, "password": self.password})
            self.assertEqual(response.status_code, 200, response.text)
            self.headers[key] = {"Authorization": "Bearer " + response.json()["access_token"]}

    def cleanup_data(self):
        with open_checkpointer() as saver:
            for thread_id in self.threads:
                saver.delete_thread(thread_id)
        ids = [person[0] for person in self.people.values()]
        with SessionFactory.begin() as session:
            session.execute(delete(AgentRunRecord).where(AgentRunRecord.employee_id.in_(ids)))
            session.execute(delete(ConversationRecord).where(ConversationRecord.employee_id.in_(ids)))
            session.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id.in_(ids)))
            session.execute(delete(LoginAttemptRecord).where(LoginAttemptRecord.username.in_(
                [person[1] for person in self.people.values()])))

    def conversation(self):
        response = self.client.post("/api/conversations", json={"title": "运行控制检查"}, headers=self.headers["owner"])
        self.assertEqual(response.status_code, 201, response.text)
        thread_id = response.json()["thread_id"]
        self.threads.add(thread_id)
        return thread_id

    def chat(self, thread_id, **body):
        return self.client.post("/api/chat", json={"thread_id": thread_id, "message": "查找车辆", **body},
                                headers=self.headers["owner"])

    def read_run(self, run_id):
        response = self.client.get(f"/api/runs/{run_id}", headers=self.headers["owner"])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_failure_resume_persists_failed_attempts_and_same_skill(self):
        thread = self.conversation()
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = RuntimeError("private-model-detail")
            first = self.chat(thread, message="帮我对比车辆")
        self.assertEqual(first.status_code, 500, first.text)
        source_id = first.json()["detail"]["run_id"]
        first_run = self.read_run(source_id)
        self.assertEqual((first_run["status"], first_run["model_calls"], first_run["skill_id"]),
                         ("failed", 1, "vehicle_compare"))
        self.assertNotIn("context_snapshot", first_run)
        self.assertEqual(self.client.post(f"/api/runs/{source_id}/resume", headers=self.headers["other"]).status_code, 404)
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.return_value = AIMessage(content="请提供要对比的两辆车。")
            resumed = self.client.post(f"/api/runs/{source_id}/resume", headers=self.headers["owner"])
        self.assertEqual(resumed.status_code, 200, resumed.text)
        current = self.read_run(resumed.json()["run_id"])
        self.assertEqual(current["parent_run_id"], source_id)
        self.assertEqual(current["turn_id"], first_run["turn_id"])
        self.assertEqual(resumed.json()["skill_id"], "vehicle_compare")
        self.assertEqual(self.client.post(f"/api/runs/{source_id}/resume", headers=self.headers["owner"]).status_code, 409)

    def test_resume_does_not_reset_budget_and_failed_turn_can_be_cancelled(self):
        thread = self.conversation()
        settings = get_settings().model_copy(update={"max_model_calls": 1})
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = RuntimeError("failure")
            with patch.object(runtime, "get_settings", return_value=settings):
                first = self.chat(thread)
            source_id = first.json()["detail"]["run_id"]
            resumed = self.client.post(f"/api/runs/{source_id}/resume", headers=self.headers["owner"])
        self.assertEqual(resumed.status_code, 422, resumed.text)
        self.assertEqual(model.invoke.call_count, 1)
        new_id = resumed.json()["detail"]["run_id"]
        cancelled = self.client.post(f"/api/runs/{new_id}/cancel", headers=self.headers["owner"])
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[AIMessage(content="可以继续。")])):
            self.assertEqual(self.chat(thread).status_code, 200)

    def test_total_timeout_is_recorded_and_unfinished_model_step_can_resume(self):
        thread = self.conversation()
        settings = get_settings().model_copy(update={"max_run_seconds": 0.5})
        timeouts = []

        def slow_model(messages, **kwargs):
            timeouts.append(kwargs["timeout"])
            time.sleep(0.6)
            return AIMessage(content="超时结果不应作为成功答案返回。")

        with patch.object(runtime, "get_settings", return_value=settings), patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = slow_model
            response = self.chat(thread)
        self.assertEqual(response.status_code, 504, response.text)
        run_id = response.json()["detail"]["run_id"]
        self.assertEqual(self.read_run(run_id)["status"], "timed_out")
        self.assertTrue(0 < timeouts[0] <= 0.5)
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[AIMessage(content="恢复成功。")])):
            resumed = self.client.post(f"/api/runs/{run_id}/resume", headers=self.headers["owner"])
        self.assertEqual(resumed.status_code, 200, resumed.text)

    def test_cooperative_cancellation_closes_outstanding_tool_call_and_is_private(self):
        thread = self.conversation()
        entered, release = Event(), Event()

        def blocked_lookup(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test gate timeout")
            return {"vehicle_id": "RUN-TEST", "title": "测试车辆", "price_cny": 100000,
                    "mileage_km": 50000, "is_demo": True}

        model = FakeMessagesListChatModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "get_vehicle", "args": {"vehicle_id": "RUN-TEST"}, "id": "blocked-tool", "type": "tool_call",
        }]), AIMessage(content="下一轮正常。")])
        with patch.object(vehicle_agent, "tool_model", model), patch.object(vehicle_tools, "load_vehicle", side_effect=blocked_lookup), \
             ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.chat, thread)
            try:
                self.assertTrue(entered.wait(5))
                records = self.client.get("/api/runs", params={"thread_id": thread}, headers=self.headers["owner"]).json()
                run_id = records[0]["run_id"]
                self.assertEqual(self.client.get(f"/api/runs/{run_id}", headers=self.headers["other"]).status_code, 404)
                self.assertEqual(self.client.post(f"/api/runs/{run_id}/cancel", headers=self.headers["other"]).status_code, 404)
                cancelling = self.client.post(f"/api/runs/{run_id}/cancel", headers=self.headers["owner"])
                self.assertEqual(cancelling.status_code, 202, cancelling.text)
                self.assertEqual(cancelling.json()["status"], "cancel_requested")
            finally:
                release.set()
            response = future.result(timeout=5)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "cancelled")
            self.assertEqual(self.read_run(run_id)["status"], "cancelled")
            self.assertEqual(self.chat(thread).status_code, 200)
        history = self.client.get(f"/api/conversations/{thread}", headers=self.headers["owner"]).json()["messages"]
        self.assertTrue(any(m.get("tool_call_id") == "blocked-tool" and "取消" in m["content"] for m in history))

    def test_skill_tools_are_restricted_in_payload_and_on_server(self):
        thread = self.conversation()
        with patch.object(vehicle_agent, "tool_model") as model, patch.object(vehicle_tools, "SessionFactory") as factory:
            model.invoke.side_effect = [AIMessage(content="", tool_calls=[{
                "name": "create_quote_draft", "args": {"vehicle_id": "RUN-TEST"}, "id": "forbidden", "type": "tool_call",
            }, {
                "name": "get_vehicle", "args": {"vehicle_id": "RUN-TEST"}, "id": "allowed-in-mixed-batch", "type": "tool_call",
            }]), AIMessage(content="请切换报价协作技能。")]
            response = self.chat(thread, message="生成报价", skill_id="inventory_match")
        self.assertEqual(response.status_code, 200, response.text)
        factory.assert_not_called()
        names = {t["function"]["name"] for t in model.invoke.call_args_list[0].kwargs["tools"]}
        self.assertEqual(names, {"get_vehicle", "search_vehicles"})
        history = self.client.get(f"/api/conversations/{thread}", headers=self.headers["owner"]).json()["messages"]
        self.assertTrue(any(m["role"] == "tool" and "无权" in m["content"] for m in history))
        skills = self.client.get("/api/skills", headers=self.headers["owner"])
        self.assertEqual(len(skills.json()), 7)
        self.assertEqual(self.chat(thread, skill_id="unknown").status_code, 400)

    def test_database_statement_timeout_is_native_and_transaction_local(self):
        with SessionFactory() as session:
            apply_query_timeout(session, 30)
            with self.assertRaises(DBAPIError):
                session.execute(text("SELECT pg_sleep(0.15)"))
            session.rollback()
        with SessionFactory() as session:
            self.assertEqual(session.scalar(text("SELECT 1")), 1)

    def test_resume_attempt_limit_is_independent_of_model_failures(self):
        thread = self.conversation()
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = RuntimeError("model unavailable")
            first = self.chat(thread)
            run_id = first.json()["detail"]["run_id"]
            for _ in range(get_settings().max_run_resumes):
                response = self.client.post(f"/api/runs/{run_id}/resume", headers=self.headers["owner"])
                self.assertEqual(response.status_code, 500, response.text)
                run_id = response.json()["detail"]["run_id"]
            response = self.client.post(f"/api/runs/{run_id}/resume", headers=self.headers["owner"])
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(model.invoke.call_count, get_settings().max_run_resumes + 1)

    def test_stale_worker_record_can_only_resume_after_deadline(self):
        thread = self.conversation()
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = RuntimeError("worker failure")
            first = self.chat(thread)
        run_id = first.json()["detail"]["run_id"]
        with SessionFactory.begin() as session:
            record = session.get(AgentRunRecord, UUID(run_id))
            record.status = "running"
        blocked = self.client.post(f"/api/runs/{run_id}/resume", headers=self.headers["owner"])
        self.assertEqual(blocked.status_code, 409, blocked.text)
        with SessionFactory.begin() as session:
            record = session.get(AgentRunRecord, UUID(run_id))
            record.deadline_at = record.started_at
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[AIMessage(content="已恢复。")])):
            resumed = self.client.post(f"/api/runs/{run_id}/resume", headers=self.headers["owner"])
        self.assertEqual(resumed.status_code, 200, resumed.text)
        source = self.read_run(run_id)
        self.assertEqual((source["status"], source["error_type"]), ("timed_out", "WorkerInterrupted"))


if __name__ == "__main__":
    unittest.main()
