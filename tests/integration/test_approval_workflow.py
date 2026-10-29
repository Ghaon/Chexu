from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from src.agent.config import Settings
from src.agent import tracing, vehicle_agent as graph_module
from src.agent.skills import SkillError
from src.api_view.app import app
from src.api_view import approvals as approval_api, auth, conversations, quotes
from src.database.checkpoints import open_checkpointer
from src.database.connection import engine as application_engine
from src.database.models import (AgentRunRecord, AgentTaskRecord, ApprovalRecord, ConversationRecord, EmployeeRecord,
                                  LoginAttemptRecord, QuoteRecord, VehicleRecord)
from src.security import hash_password
from src.services import approvals as approval_service
from src.services import conversations as conversation_service
from src.agent.tools import vehicle_tools


class FailOnceAfterApprovalModel(FakeMessagesListChatModel):
    fail_once: bool = True

    def _generate(self, *args, **kwargs):
        if self.i == 1 and self.fail_once:
            self.fail_once = False
            raise RuntimeError("private-approval-model-error")
        return super()._generate(*args, **kwargs)


class FailOnceAfterPlannedApproval(FakeMessagesListChatModel):
    fail_once: bool = True

    def _generate(self, *args, **kwargs):
        if self.i == 2 and self.fail_once:
            self.fail_once = False
            raise RuntimeError("private-nested-approval-failure")
        return super()._generate(*args, **kwargs)


class ApprovalWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(Settings().database_url.get_secret_value(),
                                   connect_args={"connect_timeout": 5}, hide_parameters=True)
        cls.factory = sessionmaker(bind=cls.engine)
        cls.password = "approval-test-password-2026"
        cls.stored_hash = hash_password(cls.password)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        application_engine.dispose()

    def setUp(self):
        self.people = {}
        self.threads = set()
        self.vehicle_code = f"APPROVE-{uuid4().hex[:20]}"
        with self.factory.begin() as session:
            for key, role in (("sales", "sales"), ("other", "sales"), ("manager", "manager")):
                employee = EmployeeRecord(username=f"approval_{uuid4().hex[:16]}", role=role,
                                          password_hash=self.stored_hash)
                session.add(employee)
                session.flush()
                self.people[key] = {"id": employee.employee_id, "username": employee.username}
            vehicle = VehicleRecord(vehicle_id=self.vehicle_code, title="审批检查车辆", price_cny=86000,
                                    mileage_km=52000, is_demo=True)
            session.add(vehicle)
            session.flush()
            self.vehicle_id = vehicle.id
        self.addCleanup(self.remove_test_data)
        for module in (auth, quotes, conversations, approval_api, approval_service, conversation_service, vehicle_tools):
            patcher = patch.object(module, "SessionFactory", self.factory)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.tokens = {}
        for key, person in self.people.items():
            response = self.client.post("/api/auth/login", json={"username": person["username"], "password": self.password})
            self.assertEqual(response.status_code, 200, response.text)
            self.tokens[key] = response.json()["access_token"]

    def remove_test_data(self):
        with open_checkpointer() as saver:
            for thread_id in self.threads:
                saver.delete_thread(thread_id)
        ids = [person["id"] for person in self.people.values()]
        with self.engine.begin() as connection:
            connection.execute(delete(AgentTaskRecord).where(AgentTaskRecord.employee_id.in_(ids)))
            connection.execute(delete(AgentRunRecord).where(AgentRunRecord.employee_id.in_(ids)))
            connection.execute(delete(ApprovalRecord).where(ApprovalRecord.vehicle_record_id == self.vehicle_id))
            connection.execute(delete(QuoteRecord).where(QuoteRecord.vehicle_record_id == self.vehicle_id))
            connection.execute(delete(ConversationRecord).where(ConversationRecord.employee_id.in_(ids)))
            connection.execute(delete(VehicleRecord).where(VehicleRecord.id == self.vehicle_id))
            connection.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id.in_(ids)))
            connection.execute(delete(LoginAttemptRecord).where(LoginAttemptRecord.username.in_(
                [person["username"] for person in self.people.values()]
            )))

    def headers(self, key="sales"):
        return {"Authorization": f"Bearer {self.tokens[key]}"}

    def test_background_task_tracks_approval_pause_and_manager_resume(self):
        from src.agent.config import get_settings
        from src.agent.task_worker import TaskWorker
        from src.services import tasks

        settings = get_settings().model_copy(update={"task_queue_name": f"approval_{uuid4().hex}"})
        with patch.object(tasks, "get_settings", return_value=settings), patch.object(graph_module, "tool_model", self.model()):
            response = self.client.post("/api/tasks", json={"message": f"给 {self.vehicle_code} 报价并提交审批"}, headers=self.headers())
            self.assertEqual(response.status_code, 202, response.text)
            task = response.json()
            self.threads.add(task["thread_id"])
            self.assertTrue(TaskWorker().run_once())
            paused = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
            self.assertEqual(paused["task"]["status"], "awaiting_approval")
            approval_id = paused["result"]["approval"]["approval_id"]
            reviewed = self.client.post(f"/api/approvals/{approval_id}/review", json={"decision": "approve"}, headers=self.headers("manager"))
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
        completed = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
        self.assertEqual(completed["task"]["status"], "completed")
        self.assertTrue(completed["result"]["approval"]["workflow_completed"])
        self.assertIsNotNone(completed["result"]["quote_id"])
        self.assertEqual(self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers("manager")).status_code, 404)

    def test_planned_quote_pauses_and_resumes_to_completed_plan_after_manager_approval(self):
        from src.agent.config import get_settings
        from src.agent.task_worker import TaskWorker
        from src.services import tasks
        from tests.test_planning import plan_call, tool_call

        settings = get_settings().model_copy(update={"task_queue_name": f"plan_approval_{uuid4().hex}"})
        model = FakeMessagesListChatModel(responses=[plan_call("quote_assist"),
            tool_call("request_quote_approval", {"vehicle_id": self.vehicle_code, "discount_cny": 2000}, "approval"),
            AIMessage(content="经理审核已通过，报价已经保存。")])
        with patch.object(tasks, "get_settings", return_value=settings), patch.object(graph_module, "tool_model", model):
            response = self.client.post("/api/tasks", json={"message": f"给 {self.vehicle_code} 优惠2000元并提交经理审批",
                "skill_id": "deal_review"}, headers=self.headers())
            self.assertEqual(response.status_code, 202, response.text)
            task = response.json()
            self.threads.add(task["thread_id"])
            self.assertTrue(TaskWorker().run_once())
            paused = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
            self.assertEqual(paused["result"]["plan"]["status"], "awaiting_approval")
            approval_id = paused["result"]["approval"]["approval_id"]
            reviewed = self.client.post(f"/api/approvals/{approval_id}/review", json={"decision": "approve"}, headers=self.headers("manager"))
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
        self.assertEqual(final["task"]["status"], "completed")
        self.assertEqual(final["result"]["plan"]["status"], "completed")
        self.assertTrue(final["result"]["plan"]["steps"][0]["result"]["approval"]["workflow_completed"])
        self.assertIsNotNone(final["result"]["quote_id"])
        self.assertEqual(self.count(QuoteRecord), 1)
        progress = self.client.get(f"/api/conversations/{task['thread_id']}/plan", headers=self.headers())
        self.assertEqual(progress.status_code, 200, progress.text)
        self.assertTrue(progress.json()["steps"][0]["result"]["approval"]["workflow_completed"])

    def test_cancellation_at_parent_join_preserves_completed_child_and_committed_quote(self):
        from src.agent.config import get_settings
        from src.agent.task_worker import TaskWorker
        from src.services import tasks
        from src.domain.execution import ExecutionCancelled
        from tests.test_planning import plan_call, tool_call

        settings = get_settings().model_copy(update={"task_queue_name": f"join_cancel_{uuid4().hex}"})
        model = FakeMessagesListChatModel(responses=[plan_call("quote_assist"),
            tool_call("request_quote_approval", {"vehicle_id": self.vehicle_code}, "approval"), AIMessage(content="已保存报价。")])
        with patch.object(tasks, "get_settings", return_value=settings), patch.object(graph_module, "tool_model", model):
            response = self.client.post("/api/tasks", json={"message": f"给 {self.vehicle_code} 报价并提交经理审批",
                "skill_id": "deal_review"}, headers=self.headers())
            self.assertEqual(response.status_code, 202, response.text)
            task = response.json()
            self.threads.add(task["thread_id"])
            TaskWorker().run_once()
            paused = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
            approval_id = paused["result"]["approval"]["approval_id"]
            with patch.object(graph_module, "collect_delegations", side_effect=ExecutionCancelled("cancel at join")):
                reviewed = self.review(approval_id)
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
        self.assertEqual(final["task"]["status"], "cancelled", final)
        self.assertIsNotNone(final["result"]["quote_id"])
        self.assertEqual(final["result"]["plan"]["steps"][0]["status"], "completed")
        self.assertIn("此前已保存的报价仍然有效", final["result"]["reply"])
        self.assertEqual(self.count(QuoteRecord), 1)

    def test_nested_reply_failure_can_resume_approval_without_saving_quote_twice(self):
        from src.agent.config import get_settings
        from src.agent.task_worker import TaskWorker
        from src.services import tasks
        from tests.test_planning import plan_call, tool_call

        settings = get_settings().model_copy(update={"task_queue_name": f"nested_retry_{uuid4().hex}"})
        model = FailOnceAfterPlannedApproval(responses=[plan_call("quote_assist"),
            tool_call("request_quote_approval", {"vehicle_id": self.vehicle_code}, "approval"), AIMessage(content="已保存报价。")])
        with patch.object(tasks, "get_settings", return_value=settings), patch.object(graph_module, "tool_model", model):
            response = self.client.post("/api/tasks", json={"message": f"给 {self.vehicle_code} 报价并提交经理审批",
                "skill_id": "deal_review"}, headers=self.headers())
            self.assertEqual(response.status_code, 202, response.text)
            task = response.json()
            self.threads.add(task["thread_id"])
            TaskWorker().run_once()
            paused = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
            approval_id = paused["result"]["approval"]["approval_id"]
            first = self.review(approval_id)
            self.assertEqual(first.json()["execution_status"], "failed")
            second = self.review(approval_id)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()["execution_status"], "completed", second.text)
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
        self.assertEqual(final["task"]["status"], "completed", final)
        self.assertEqual(final["result"]["plan"]["status"], "completed")
        self.assertTrue(final["result"]["plan"]["steps"][0]["result"]["approval"]["workflow_completed"])
        self.assertEqual(self.count(QuoteRecord), 1)

    def test_nested_committed_quote_cannot_be_revised_and_survives_parent_cancellation(self):
        from src.agent.config import get_settings
        from src.agent.task_worker import TaskWorker
        from src.services import tasks
        from tests.test_planning import plan_call, tool_call

        settings = get_settings().model_copy(update={"task_queue_name": f"nested_cancel_{uuid4().hex}"})
        model = FailOnceAfterPlannedApproval(responses=[plan_call("quote_assist"),
            tool_call("request_quote_approval", {"vehicle_id": self.vehicle_code}, "approval"), AIMessage(content="已保存报价。")])
        with patch.object(tasks, "get_settings", return_value=settings), patch.object(graph_module, "tool_model", model):
            response = self.client.post("/api/tasks", json={"message": f"给 {self.vehicle_code} 报价并提交经理审批",
                "skill_id": "deal_review"}, headers=self.headers())
            self.assertEqual(response.status_code, 202, response.text)
            task = response.json()
            self.threads.add(task["thread_id"])
            TaskWorker().run_once()
            paused = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
            approval_id = paused["result"]["approval"]["approval_id"]
            action = {"action": "revise", "expected_revision": 1, "expected_turn_id": paused["result"]["plan"]["turn_id"],
                      "message": "改成其他车辆。"}
            self.assertEqual(self.client.post(f"/api/conversations/{task['thread_id']}/plan/actions", json=action,
                headers=self.headers()).status_code, 409)
            review = self.client.post(f"/api/approvals/{approval_id}/review", json={"decision": "approve"}, headers=self.headers("manager"))
            self.assertEqual(review.status_code, 200, review.text)
            self.assertEqual(review.json()["execution_status"], "failed")
        failed = self.client.get(f"/api/tasks/{task['task_id']}", headers=self.headers()).json()
        cancelled = self.client.post(f"/api/runs/{failed['run_id']}/cancel", headers=self.headers())
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
        self.assertEqual(final["task"]["status"], "cancelled")
        self.assertIsNotNone(final["result"]["quote_id"])
        self.assertIn("此前已保存的报价仍然有效", final["result"]["reply"])
        self.assertEqual(self.count(QuoteRecord), 1)

    def test_background_approval_cancel_updates_final_task_result(self):
        from src.agent.config import get_settings
        from src.agent.task_worker import TaskWorker
        from src.services import tasks

        settings = get_settings().model_copy(update={"task_queue_name": f"cancel_{uuid4().hex}"})
        with patch.object(tasks, "get_settings", return_value=settings), patch.object(graph_module, "tool_model", self.model()):
            response = self.client.post("/api/tasks", json={"message": f"给 {self.vehicle_code} 报价并提交审批"}, headers=self.headers())
            self.assertEqual(response.status_code, 202, response.text)
            task = response.json()
            self.threads.add(task["thread_id"])
            self.assertTrue(TaskWorker().run_once())
        paused = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
        approval_id = paused["result"]["approval"]["approval_id"]
        cancelled = self.client.post(f"/api/approvals/{approval_id}/cancel", headers=self.headers())
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers()).json()
        self.assertEqual(final["task"]["status"], "cancelled")
        self.assertTrue(final["result"]["approval"]["workflow_completed"])
        self.assertEqual(final["result"]["approval"]["status"], "cancelled")
        self.assertIsNone(final["result"]["quote_id"])

    def test_pending_approval_can_cancel_without_model_or_enabled_skill(self):
        with patch.object(graph_module, "tool_model", self.model()):
            paused = self.client.post("/api/chat", json={"message": f"给 {self.vehicle_code} 报价并提交审批"}, headers=self.headers())
        self.assertEqual(paused.status_code, 200, paused.text)
        self.threads.add(paused.json()["thread_id"])
        approval_id = paused.json()["approval"]["approval_id"]
        with patch("src.agent.runtime.get_skill_registry", side_effect=SkillError("技能已停用")), \
             patch.object(graph_module, "tool_model") as model:
            response = self.client.post(f"/api/approvals/{approval_id}/cancel", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["approval"]["status"], "cancelled")
        self.assertEqual(response.json()["execution_status"], "completed")
        model.invoke.assert_not_called()

    def test_cancel_failed_reply_keeps_already_committed_quote(self):
        with patch.object(graph_module, "tool_model", self.model(FailOnceAfterApprovalModel)):
            paused = self.client.post("/api/chat", json={"message": f"给 {self.vehicle_code} 报价并提交审批"}, headers=self.headers())
            self.assertEqual(paused.status_code, 200, paused.text)
            self.threads.add(paused.json()["thread_id"])
            approval_id = paused.json()["approval"]["approval_id"]
            reviewed = self.client.post(f"/api/approvals/{approval_id}/review", json={"decision": "approve"},
                                        headers=self.headers("manager"))
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["execution_status"], "failed")
        quote_id = reviewed.json()["approval"]["quote_id"]
        run_id = reviewed.json()["run_id"]
        cancelled = self.client.post(f"/api/runs/{run_id}/cancel", headers=self.headers())
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        task = self.client.get(f"/api/approvals/{approval_id}", headers=self.headers()).json()
        self.assertEqual((task["status"], task["quote_id"]), ("completed", quote_id))
        history = self.client.get(f"/api/conversations/{paused.json()['thread_id']}", headers=self.headers()).json()
        self.assertIn("此前已保存的报价仍然有效", history["messages"][-1]["content"])

    def model(self, cls=FakeMessagesListChatModel):
        return cls(responses=[AIMessage(content="", tool_calls=[{
            "name": "request_quote_approval", "args": {"vehicle_id": self.vehicle_code, "discount_cny": 2000},
            "id": "approval-call", "type": "tool_call",
        }]), AIMessage(content="报价审批结果已处理。")])

    def submit(self):
        thread_id = str(uuid4())
        self.threads.add(thread_id)
        response = self.client.post("/api/chat", json={
            "thread_id": thread_id, "message": "私密聊天备注：请提交这辆车的报价审批，优惠2000元。",
        }, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "awaiting_approval")
        self.assertEqual(response.json()["approval"]["status"], "pending")
        return response.json()

    def count(self, model):
        with self.factory() as session:
            return session.scalar(select(func.count()).select_from(model).where(model.vehicle_record_id == self.vehicle_id))

    def review(self, approval_id, decision="approve", key="manager"):
        return self.client.post(f"/api/approvals/{approval_id}/review", json={"decision": decision}, headers=self.headers(key))

    def test_pause_review_resume_and_repeated_review_create_one_confirmed_quote(self):
        with patch.object(graph_module, "tool_model", self.model()), self.assertLogs(tracing.logger, "INFO") as logs:
            paused = self.submit()
            approval_id = paused["approval"]["approval_id"]
            self.assertEqual(self.count(QuoteRecord), 0)
            self.assertEqual(self.count(ApprovalRecord), 1)
            pending = self.client.get("/api/approvals?status=pending", headers=self.headers("manager"))
            self.assertIn(approval_id, [a["approval_id"] for a in pending.json()["approvals"]])
            history = self.client.get(f"/api/conversations/{paused['thread_id']}", headers=self.headers())
            self.assertEqual(history.json()["pending_approval_id"], approval_id)
            reviewed = self.review(approval_id)
            self.assertEqual(reviewed.status_code, 200, reviewed.text)
            body = reviewed.json()
            self.assertEqual(body["execution_status"], "completed")
            self.assertEqual(body["approval"]["status"], "completed")
            self.assertTrue(body["approval"]["workflow_completed"])
            self.assertNotIn("私密聊天备注", reviewed.text)
            repeated = self.review(approval_id)
            self.assertEqual(repeated.status_code, 200, repeated.text)
            self.assertEqual(repeated.json()["approval"]["quote_id"], body["approval"]["quote_id"])
        self.assertTrue(any('"event": "run.paused"' in line for line in logs.output))
        self.assertEqual(self.count(QuoteRecord), 1)
        self.assertEqual(self.count(ApprovalRecord), 1)
        quote = self.client.get(f"/api/quotes/{body['approval']['quote_id']}", headers=self.headers())
        self.assertEqual(quote.status_code, 200, quote.text)
        self.assertEqual(quote.json()["quoted_price_cny"], 84000)
        self.assertEqual(quote.json()["status"], "confirmed")
        self.assertEqual(quote.json()["created_by_employee_id"], str(self.people["sales"]["id"]))
        self.assertEqual(quote.json()["confirmed_by_employee_id"], str(self.people["manager"]["id"]))
        self.assertEqual(self.client.get(f"/api/conversations/{paused['thread_id']}", headers=self.headers("manager")).status_code, 404)

    def test_reject_cancel_and_expiry_resume_without_writing_quotes(self):
        for action, expected in (("reject", "rejected"), ("cancel", "cancelled"), ("expire", "expired")):
            with self.subTest(action=action), patch.object(graph_module, "tool_model", self.model()), self.assertLogs(tracing.logger, "INFO"):
                paused = self.submit()
                approval_id = paused["approval"]["approval_id"]
                if action == "cancel":
                    result = self.client.post(f"/api/approvals/{approval_id}/cancel", headers=self.headers())
                else:
                    if action == "expire":
                        with self.factory.begin() as session:
                            session.get(ApprovalRecord, UUID(approval_id)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    result = self.review(approval_id, decision="reject" if action == "reject" else "approve")
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["approval"]["status"], expected)
                self.assertEqual(result.json()["execution_status"], "completed")
                self.assertIsNone(result.json()["approval"]["quote_id"])
        self.assertEqual(self.count(QuoteRecord), 0)

    def test_review_permissions_payload_integrity_and_price_changes_are_enforced(self):
        with patch.object(graph_module, "tool_model", self.model()), self.assertLogs(tracing.logger, "INFO"):
            paused = self.submit()
            approval_id = paused["approval"]["approval_id"]
            self.assertEqual(self.review(approval_id, key="sales").status_code, 403)
            self.assertEqual(self.client.get(f"/api/approvals/{approval_id}", headers=self.headers("other")).status_code, 404)
            tampered = self.client.post(f"/api/approvals/{approval_id}/review", json={
                "decision": "approve", "discount_cny": 1,
            }, headers=self.headers("manager"))
            self.assertEqual(tampered.status_code, 422)
            blocked = self.client.post("/api/chat", json={"thread_id": paused["thread_id"], "message": "先回答其他问题"},
                                       headers=self.headers())
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.json()["detail"]["code"], "approval_pending")
            with self.factory.begin() as session:
                session.get(VehicleRecord, self.vehicle_id).price_cny = 90000
            reviewed = self.review(approval_id)
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["approval"]["status"], "failed")
        self.assertIn("售价已变化", reviewed.json()["approval"]["failure_reason"])
        self.assertEqual(self.count(QuoteRecord), 0)

    def test_model_failure_after_quote_commit_can_resume_without_duplicate_write(self):
        with patch.object(graph_module, "tool_model", self.model(FailOnceAfterApprovalModel)), self.assertLogs(tracing.logger, "INFO") as logs:
            paused = self.submit()
            approval_id = paused["approval"]["approval_id"]
            first = self.review(approval_id)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["execution_status"], "failed")
            self.assertEqual(first.json()["approval"]["status"], "completed")
            self.assertEqual(first.json()["approval"]["last_resume_error"], "RuntimeError")
            self.assertEqual(self.count(QuoteRecord), 1)
            retried = self.review(approval_id)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["execution_status"], "completed")
        self.assertEqual(retried.json()["approval"]["resume_attempts"], 2)
        self.assertIsNone(retried.json()["approval"]["last_resume_error"])
        self.assertEqual(first.json()["approval"]["quote_id"], retried.json()["approval"]["quote_id"])
        self.assertEqual(self.count(QuoteRecord), 1)
        self.assertNotIn("private-approval-model-error", " ".join(logs.output) + first.text)

    def test_pending_approval_blocks_delete_and_cancelled_audit_survives_chat_deletion(self):
        with patch.object(graph_module, "tool_model", self.model()), self.assertLogs(tracing.logger, "INFO"):
            paused = self.submit()
            approval_id = paused["approval"]["approval_id"]
            path = f"/api/conversations/{paused['thread_id']}"
            self.assertEqual(self.client.delete(path, headers=self.headers()).status_code, 409)
            cancelled = self.client.post(f"/api/approvals/{approval_id}/cancel", headers=self.headers())
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
        deleted = self.client.delete(path, headers=self.headers())
        self.assertEqual(deleted.status_code, 204, deleted.text)
        audit = self.client.get(f"/api/approvals/{approval_id}", headers=self.headers())
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertEqual(audit.json()["status"], "cancelled")
        self.assertIsNone(audit.json()["thread_id"])

    def test_concurrent_review_and_manual_quote_save_do_not_deadlock(self):
        with patch.object(graph_module, "tool_model", self.model()), self.assertLogs(tracing.logger, "INFO"):
            paused = self.submit()
            approval_id = paused["approval"]["approval_id"]
            ready = Barrier(2)

            def review():
                with TestClient(app) as client:
                    ready.wait(timeout=10)
                    return client.post(f"/api/approvals/{approval_id}/review", json={"decision": "approve"},
                                       headers=self.headers("manager"))

            def save():
                with TestClient(app) as client:
                    ready.wait(timeout=10)
                    return client.post("/api/quotes", json={
                        "vehicle_id": self.vehicle_code, "expected_price_cny": 86000,
                    }, headers=self.headers())

            with ThreadPoolExecutor(max_workers=2) as executor:
                reviewing = executor.submit(review)
                saving = executor.submit(save)
                reviewed = reviewing.result(timeout=20)
                saved = saving.result(timeout=20)
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["execution_status"], "completed")
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(self.count(QuoteRecord), 2)


if __name__ == "__main__":
    unittest.main()
