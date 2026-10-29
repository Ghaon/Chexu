from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine, delete, insert, select, text
from sqlalchemy.orm import sessionmaker

from src.agent.config import Settings
from src.agent import vehicle_agent as graph_module
from src.agent import tracing
from src.api_view.app import app
from src.api_view import auth, conversations, quotes
from src.database import maintenance
from src.database.checkpoints import open_checkpointer
from src.database.connection import engine as application_engine
from src.database.models import (AgentRunRecord, ConversationRecord, EmployeeRecord, EmployeeTokenRecord,
                                  LoginAttemptRecord, QuoteRecord, VehicleRecord)
from src.domain.quotes import build_quote_draft
from src.security import hash_password
from src.version import STAGE
from src.services import conversations as conversation_service


class AccessBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings()
        cls.engine = create_engine(cls.settings.database_url.get_secret_value(),
                                   connect_args={"connect_timeout": 5}, hide_parameters=True)
        cls.factory = sessionmaker(bind=cls.engine)
        cls.password = "access-boundary-password-2026"
        cls.stored_hash = hash_password(cls.password)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        application_engine.dispose()

    def setUp(self):
        self.people = {}
        self.thread_ids = set()
        self.vehicle_code = f"ACCESS-{uuid4().hex[:20]}"
        with self.factory.begin() as session:
            for key, role in (("a", "sales"), ("b", "sales"), ("manager", "manager")):
                employee = EmployeeRecord(username=f"access_{uuid4().hex[:16]}", role=role,
                                          password_hash=self.stored_hash)
                session.add(employee)
                session.flush()
                self.people[key] = {"id": employee.employee_id, "username": employee.username}
            vehicle = VehicleRecord(vehicle_id=self.vehicle_code, title="权限边界检查车辆",
                                    price_cny=86000, mileage_km=52000, is_demo=True)
            session.add(vehicle)
            session.flush()
            self.vehicle_id = vehicle.id
        self.addCleanup(self.remove_test_data)
        for module in (auth, quotes, conversations, conversation_service):
            patcher = patch.object(module, "SessionFactory", self.factory)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.tokens = {key: self.login(key) for key in self.people}

    def remove_test_data(self):
        with open_checkpointer() as saver:
            for thread_id in self.thread_ids:
                saver.delete_thread(thread_id)
        employee_ids = [person["id"] for person in self.people.values()]
        with self.engine.begin() as connection:
            connection.execute(delete(AgentRunRecord).where(AgentRunRecord.employee_id.in_(employee_ids)))
            connection.execute(delete(QuoteRecord).where(QuoteRecord.vehicle_record_id == self.vehicle_id))
            connection.execute(delete(VehicleRecord).where(VehicleRecord.id == self.vehicle_id))
            connection.execute(delete(ConversationRecord).where(ConversationRecord.employee_id.in_(employee_ids)))
            connection.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id.in_(employee_ids)))
            connection.execute(delete(LoginAttemptRecord).where(LoginAttemptRecord.username.in_(
                [person["username"] for person in self.people.values()]
            )))

    def headers(self, key):
        return {"Authorization": f"Bearer {self.tokens[key]}"}

    def login(self, key, password=None):
        response = self.client.post("/api/auth/login", json={
            "username": self.people[key]["username"], "password": password or self.password,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def create_conversation(self, key="a", title="权限检查会话"):
        response = self.client.post("/api/conversations", json={"title": title}, headers=self.headers(key))
        self.assertEqual(response.status_code, 201, response.text)
        thread_id = response.json()["thread_id"]
        self.thread_ids.add(thread_id)
        return thread_id

    def test_business_routes_require_login_and_quotes_follow_creator_permissions(self):
        payload = {"vehicle_id": self.vehicle_code, "expected_price_cny": 86000, "discount_cny": 2000}
        for method, path, body in (
            ("POST", "/api/chat", {"message": "查车"}),
            ("POST", "/api/vehicles", {}), ("POST", "/api/quotes", payload),
            ("GET", f"/api/quotes/{uuid4()}", None),
            ("GET", "/api/conversations", None), ("POST", "/api/conversations", {}),
            ("GET", f"/api/conversations/{uuid4()}", None),
            ("DELETE", f"/api/conversations/{uuid4()}", None),
        ):
            with self.subTest(path=path, method=method):
                response = self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.client.get("/health").json()["stage"], STAGE)
        created = self.client.post("/api/quotes", json=payload, headers=self.headers("a"))
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["created_by_employee_id"], str(self.people["a"]["id"]))
        path = f"/api/quotes/{created.json()['quote_id']}"
        self.assertEqual(self.client.get(path, headers=self.headers("a")).status_code, 200)
        self.assertEqual(self.client.get(path, headers=self.headers("b")).status_code, 404)
        self.assertEqual(self.client.get(path, headers=self.headers("manager")).status_code, 200)
        forged = self.client.post("/api/quotes", json=payload | {
            "created_by_employee_id": str(self.people["b"]["id"]),
        }, headers=self.headers("a"))
        self.assertEqual(forged.status_code, 422)
        draft = build_quote_draft(vehicle_id=self.vehicle_code, vehicle_title="历史报价",
                                  listed_price_cny=86000, discount_cny=0, is_demo=True)
        with self.factory.begin() as session:
            legacy = QuoteRecord(vehicle_record_id=self.vehicle_id, **draft.model_dump())
            session.add(legacy)
            session.flush()
            legacy_id = legacy.quote_id
        self.assertEqual(self.client.get(f"/api/quotes/{legacy_id}", headers=self.headers("a")).status_code, 404)
        self.assertEqual(self.client.get(f"/api/quotes/{legacy_id}", headers=self.headers("manager")).status_code, 200)

    def test_conversation_history_listing_continuation_and_deletion_are_private(self):
        thread_id = self.create_conversation()
        model = FakeMessagesListChatModel(responses=[
            AIMessage(content="已记录预算。"), AIMessage(content="预算是十万元。"), AIMessage(content="不应执行。"),
        ])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO"):
            first = self.client.post("/api/chat", json={"thread_id": thread_id, "message": "预算十万元"},
                                     headers=self.headers("a"))
            self.assertEqual(first.status_code, 200, first.text)
            for key in ("b", "manager"):
                for method, path, body in (
                    ("GET", f"/api/conversations/{thread_id}", None),
                    ("DELETE", f"/api/conversations/{thread_id}", None),
                    ("POST", "/api/chat", {"thread_id": thread_id, "message": "窃取历史"}),
                ):
                    response = self.client.request(method, path, json=body, headers=self.headers(key))
                    self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(model.i, 1)
            second = self.client.post("/api/chat", json={"thread_id": thread_id, "message": "预算多少"},
                                      headers=self.headers("a"))
            self.assertEqual(second.status_code, 200, second.text)
        history = self.client.get(f"/api/conversations/{thread_id}", headers=self.headers("a"))
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual([m["content"] for m in history.json()["messages"] if m["role"] == "user"],
                         ["预算十万元", "预算多少"])
        self.assertEqual(self.client.get("/api/conversations", headers=self.headers("b")).json()["conversations"], [])
        self.create_conversation(title="第二个会话")
        listed = self.client.get("/api/conversations?limit=1", headers=self.headers("a"))
        self.assertTrue(listed.json()["has_more"])
        self.assertEqual(len(listed.json()["conversations"]), 1)
        deleted = self.client.delete(f"/api/conversations/{thread_id}", headers=self.headers("a"))
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(self.client.get(f"/api/conversations/{thread_id}", headers=self.headers("a")).status_code, 404)
        with open_checkpointer() as saver:
            self.assertIsNone(saver.get_tuple({"configurable": {"thread_id": thread_id}}))

    def test_unowned_legacy_checkpoint_cannot_be_claimed_and_busy_conversation_cannot_be_deleted(self):
        legacy_id = str(uuid4())
        self.thread_ids.add(legacy_id)
        with open_checkpointer(legacy_id) as saver:
            graph_module.build_vehicle_agent(saver).update_state(
                {"configurable": {"thread_id": legacy_id}},
                {"messages": [HumanMessage(content="未登记归属的旧消息"), AIMessage(content="旧回复")]},
                as_node="finish",
            )
        model = FakeMessagesListChatModel(responses=[AIMessage(content="不应执行。")])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO"):
            response = self.client.post("/api/chat", json={"thread_id": legacy_id, "message": "认领旧会话"},
                                        headers=self.headers("a"))
        self.assertEqual(response.status_code, 404, response.text)
        with self.factory() as session:
            self.assertIsNone(session.get(ConversationRecord, UUID(legacy_id)))
        thread_id = self.create_conversation()
        with open_checkpointer(thread_id):
            blocked = self.client.delete(f"/api/conversations/{thread_id}", headers=self.headers("a"))
            self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(self.client.delete(f"/api/conversations/{thread_id}", headers=self.headers("a")).status_code, 204)

    def test_login_lockout_and_password_change_revoke_all_employee_tokens(self):
        settings = self.settings.model_copy(update={"login_max_failures": 3, "login_lock_minutes": 1})
        with patch.object(auth, "get_settings", return_value=settings):
            for expected in (401, 401, 429):
                failed = self.client.post("/api/auth/login", json={
                    "username": self.people["a"]["username"], "password": "wrong-password",
                })
                self.assertEqual(failed.status_code, expected, failed.text)
            locked = self.client.post("/api/auth/login", json={
                "username": self.people["a"]["username"], "password": self.password,
            })
            self.assertEqual(locked.status_code, 429)
            self.assertGreater(int(locked.headers["retry-after"]), 0)
        with self.factory.begin() as session:
            session.get(LoginAttemptRecord, self.people["a"]["username"]).locked_until = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        other_token = self.login("a")
        wrong = self.client.post("/api/auth/change-password", json={
            "current_password": "wrong-password", "new_password": "new-access-password-2026",
        }, headers=self.headers("a"))
        self.assertEqual(wrong.status_code, 400)
        changed = self.client.post("/api/auth/change-password", json={
            "current_password": self.password, "new_password": "new-access-password-2026",
        }, headers=self.headers("a"))
        self.assertEqual(changed.status_code, 204, changed.text)
        for token in (self.tokens["a"], other_token):
            self.assertEqual(self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.headers("manager")).status_code, 200)
        old = self.client.post("/api/auth/login", json={"username": self.people["a"]["username"], "password": self.password})
        self.assertEqual(old.status_code, 401)
        self.login("a", password="new-access-password-2026")

    def test_security_cleanup_removes_only_expired_and_stale_rows(self):
        now = datetime.now(timezone.utc)
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                for table in ("employee_tokens", "login_attempts"):
                    connection.execute(text(f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP"))
                connection.execute(insert(EmployeeTokenRecord), [
                    {"token_hash": "a" * 64, "employee_id": self.people["a"]["id"], "expires_at": now - timedelta(days=1)},
                    {"token_hash": "b" * 64, "employee_id": self.people["a"]["id"], "expires_at": now + timedelta(days=1)},
                ])
                connection.execute(insert(LoginAttemptRecord), [
                    {"username": "stale", "updated_at": now - timedelta(days=8)},
                    {"username": "recent", "updated_at": now},
                ])
                factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
                with patch.object(maintenance, "SessionFactory", factory):
                    self.assertEqual(maintenance.prune_auth(), (1, 1))
                with factory() as session:
                    self.assertIsNotNone(session.get(EmployeeTokenRecord, "b" * 64))
                    self.assertIsNotNone(session.get(LoginAttemptRecord, "recent"))
            finally:
                transaction.rollback()


if __name__ == "__main__":
    unittest.main()
