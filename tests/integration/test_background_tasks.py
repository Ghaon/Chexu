from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pydantic import PrivateAttr
from sqlalchemy import delete, select

from src.agent import vehicle_agent
from src.agent.config import get_settings
from src.agent.task_worker import TaskWorker
from src.api_view.app import app
from src.database.checkpoints import open_checkpointer
from src.database.connection import SessionFactory, engine
from src.database.models import (AgentRunRecord, AgentTaskRecord, ConversationRecord, EmployeeRecord,
                                  LoginAttemptRecord, VehicleRecord)
from src.security import hash_password
from src.services import tasks


class BlockingModel(FakeMessagesListChatModel):
    _entered: Event = PrivateAttr(default_factory=Event)
    _release: Event = PrivateAttr(default_factory=Event)

    def _generate(self, *args, **kwargs):
        self._entered.set()
        if not self._release.wait(8):
            raise RuntimeError("test gate timeout")
        return super()._generate(*args, **kwargs)


class FailOnceInQuoteStep(FakeMessagesListChatModel):
    fail_once: bool = True

    def _generate(self, *args, **kwargs):
        if self.i == 3 and self.fail_once:
            self.fail_once = False
            raise RuntimeError("private-planned-model-failure")
        return super()._generate(*args, **kwargs)


class BlockingQuoteStep(BlockingModel):
    def _generate(self, *args, **kwargs):
        if self.i == 3:
            return super()._generate(*args, **kwargs)
        return FakeMessagesListChatModel._generate(self, *args, **kwargs)


class BackgroundTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "background-task-password-2026"
        cls.password_hash = hash_password(cls.password)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        self.settings = get_settings().model_copy(update={"task_queue_name": f"test_{uuid4().hex}", "task_poll_seconds": 0.1})
        patcher = patch.object(tasks, "get_settings", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.people, self.threads = {}, set()
        with SessionFactory.begin() as session:
            for key, role in (("owner", "sales"), ("other", "manager")):
                record = EmployeeRecord(username=f"task_{uuid4().hex[:20]}", role=role, password_hash=self.password_hash)
                session.add(record)
                session.flush()
                self.people[key] = (record.employee_id, record.username)
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
            for thread in self.threads:
                saver.delete_thread(thread)
        ids = [person[0] for person in self.people.values()]
        with SessionFactory.begin() as session:
            session.execute(delete(AgentTaskRecord).where(AgentTaskRecord.employee_id.in_(ids)))
            session.execute(delete(AgentRunRecord).where(AgentRunRecord.employee_id.in_(ids)))
            session.execute(delete(ConversationRecord).where(ConversationRecord.employee_id.in_(ids)))
            session.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id.in_(ids)))
            session.execute(delete(LoginAttemptRecord).where(LoginAttemptRecord.username.in_([p[1] for p in self.people.values()])))

    def submit(self, **body):
        response = self.client.post("/api/tasks", json={"message": "PRIVATE_TASK_INPUT", **body}, headers=self.headers["owner"])
        self.assertEqual(response.status_code, 202, response.text)
        task = response.json()
        self.threads.add(task["thread_id"])
        return task

    def get_task(self, task_id):
        response = self.client.get(f"/api/tasks/{task_id}", headers=self.headers["owner"])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def plan_vehicle(self):
        code = f"PLAN-{uuid4().hex[:20]}"
        with SessionFactory.begin() as session:
            session.add(VehicleRecord(vehicle_id=code, title="规划恢复检查车辆", price_cny=86000,
                                      mileage_km=52000, is_demo=True))

        def cleanup():
            with SessionFactory.begin() as session:
                session.execute(delete(VehicleRecord).where(VehicleRecord.vehicle_id == code))

        self.addCleanup(cleanup)
        return code

    def post_plan_action(self, task, action, message, *, command_id=None):
        progress = self.client.get(f"/api/conversations/{task['thread_id']}/plan", headers=self.headers["owner"])
        self.assertEqual(progress.status_code, 200, progress.text)
        body = {"command_id": str(command_id or uuid4()), "expected_revision": progress.json()["revision"],
                "expected_turn_id": progress.json()["turn_id"], "action": action, "message": message}
        response = self.client.post(f"/api/conversations/{task['thread_id']}/plan/actions", json=body, headers=self.headers["owner"])
        self.assertEqual(response.status_code, 202, response.text)
        return response.json(), body

    def test_parallel_child_failure_resumes_without_repeating_completed_sibling(self):
        from collections import Counter
        from tests.test_subagents import ParallelInventoryModel
        from src.agent.tools import vehicle_tools

        codes = [self.plan_vehicle(), self.plan_vehicle()]
        model = ParallelInventoryModel(responses=[AIMessage(content="unused")], vehicle_codes=codes, fail_code=codes[1])
        with patch.object(vehicle_agent, "tool_model", model), \
             patch.object(vehicle_tools, "load_vehicle", wraps=vehicle_tools.load_vehicle) as read:
            task = self.submit(skill_id="deal_review")
            TaskWorker().run_once()
            failed = self.get_task(task["task_id"])
            self.assertEqual(failed["status"], "failed")
            resumed = self.client.post(f"/api/runs/{failed['run_id']}/resume", headers=self.headers["owner"])
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(Counter(call.args[0] for call in read.call_args_list), Counter(codes))
            self.assertEqual(model._planning_calls, 1)
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual([step["status"] for step in final["result"]["plan"]["steps"]], ["completed", "completed"])
        with SessionFactory() as session:
            runs = session.scalars(select(AgentRunRecord).where(AgentRunRecord.turn_id == UUID(task["run_id"]))).all()
            self.assertEqual(sum(record.model_calls for record in runs), 6)
            self.assertEqual(sum(record.tool_calls for record in runs), 2)

    def test_running_goal_revision_is_applied_after_current_batch(self):
        from tests.test_planning import plan_call, tool_call

        old_code, new_code = self.plan_vehicle(), self.plan_vehicle()
        model = BlockingQuoteStep(responses=[plan_call("inventory_match", "quote_assist"),
            tool_call("get_vehicle", {"vehicle_id": old_code}, "initial"), AIMessage(content="已查询原候选车辆。"),
            tool_call("create_quote_draft", {"vehicle_id": old_code}, "old-preview"), AIMessage(content="原车辆报价预览已生成。"),
            plan_call("quote_assist"), tool_call("create_quote_draft", {"vehicle_id": new_code}, "new-preview"),
            AIMessage(content="已按新候选车辆重新生成报价预览。")])
        with patch.object(vehicle_agent, "tool_model", model), ThreadPoolExecutor(max_workers=1) as pool:
            task = self.submit(skill_id="deal_review")
            future = pool.submit(TaskWorker().run_once)
            try:
                self.assertTrue(model._entered.wait(4))
                action, _ = self.post_plan_action(task, "revise", f"候选车辆改为 {new_code}，只需要报价预览。")
                self.assertEqual(action["status"], "queued")
            finally:
                model._release.set()
            self.assertTrue(future.result(timeout=10))
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(final["task"]["status"], "completed", final)
        self.assertEqual(final["task"]["run_id"], task["run_id"])
        self.assertEqual(final["result"]["plan"]["revision"], 2)
        self.assertEqual(final["result"]["quote_draft"]["vehicle_id"], new_code)

    def test_plan_continuation_reuses_task_and_turn_and_is_idempotent(self):
        from tests.test_planning import plan_call, tool_call

        code = self.plan_vehicle()
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[
            plan_call("inventory_match", "quote_assist"), AIMessage(content="请提供车辆编号。")])):
            task = self.submit(skill_id="deal_review")
            self.assertTrue(TaskWorker().run_once())
        response = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(response["result"]["plan"]["status"], "needs_input")
        turn_id = response["result"]["plan"]["turn_id"]
        action, body = self.post_plan_action(task, "continue", f"车辆编号是 {code}，请继续生成报价预览。")
        self.assertEqual(action["task_id"], task["task_id"])
        duplicate = self.client.post(f"/api/conversations/{task['thread_id']}/plan/actions", json=body, headers=self.headers["owner"])
        self.assertEqual(duplicate.status_code, 202, duplicate.text)
        self.assertEqual(duplicate.json()["command_id"], action["command_id"])
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[
            tool_call("get_vehicle", {"vehicle_id": code}, "continued-read"), AIMessage(content="已找到候选车辆。"),
            tool_call("create_quote_draft", {"vehicle_id": code}, "continued-quote"), AIMessage(content="已生成报价预览。")])):
            self.assertTrue(TaskWorker().run_once())
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(final["task"]["status"], "completed", final)
        self.assertEqual(final["result"]["plan"]["status"], "completed")
        self.assertEqual(final["result"]["plan"]["revision"], 2)
        self.assertEqual(final["result"]["plan"]["turn_id"], turn_id)
        self.assertNotEqual(final["task"]["run_id"], task["run_id"])
        action_url = f"/api/conversations/{task['thread_id']}/plan/actions/{action['command_id']}"
        self.assertEqual(self.client.get(action_url, headers=self.headers["owner"]).json()["status"], "applied")
        self.assertEqual(self.client.get(action_url, headers=self.headers["other"]).status_code, 404)
        stale = {**body, "command_id": str(uuid4())}
        self.assertEqual(self.client.post(f"/api/conversations/{task['thread_id']}/plan/actions", json=stale,
            headers=self.headers["owner"]).status_code, 409)
        history = self.client.get(f"/api/conversations/{task['thread_id']}", headers=self.headers["owner"]).json()["messages"]
        self.assertFalse(any(item.get("tool_name") in {"get_vehicle", "create_quote_draft"} for item in history))

    def test_old_turn_action_is_rejected_even_if_new_plan_has_same_revision(self):
        from tests.test_planning import plan_call

        model = FakeMessagesListChatModel(responses=[plan_call("inventory_match"), AIMessage(content="请补充车辆编号。")])
        with patch.object(vehicle_agent, "tool_model", model):
            original = self.submit(skill_id="deal_review")
            TaskWorker().run_once()
            old_plan = self.client.get(f"/api/conversations/{original['thread_id']}/plan", headers=self.headers["owner"]).json()
            newer = self.submit(skill_id="deal_review", thread_id=original["thread_id"])
            TaskWorker().run_once()
        body = {"action": "continue", "expected_revision": old_plan["revision"], "expected_turn_id": old_plan["turn_id"],
                "message": "这是旧任务的补充信息。"}
        response = self.client.post(f"/api/conversations/{newer['thread_id']}/plan/actions", json=body, headers=self.headers["owner"])
        self.assertEqual(response.status_code, 409, response.text)

    def test_explicit_replan_keeps_completed_steps_and_charges_original_turn(self):
        from tests.test_planning import plan_call, tool_call
        from src.agent.tools import vehicle_tools

        code = self.plan_vehicle()
        initial = FailOnceInQuoteStep(responses=[plan_call("inventory_match", "quote_assist"),
            tool_call("get_vehicle", {"vehicle_id": code}, "inventory"), AIMessage(content="已查到候选车辆。"),
            tool_call("create_quote_draft", {"vehicle_id": code}, "unused"), AIMessage(content="unused")])
        with patch.object(vehicle_agent, "tool_model", initial):
            task = self.submit(skill_id="deal_review")
            TaskWorker().run_once()
        before = self.client.get(f"/api/conversations/{task['thread_id']}/plan", headers=self.headers["owner"]).json()
        completed_agent_id = before["steps"][0]["agent_id"]
        self.post_plan_action(task, "replan", "保留已完成的库存查询，只重新规划剩余报价预览。")
        replacement = FakeMessagesListChatModel(responses=[plan_call("quote_assist"),
            tool_call("create_quote_draft", {"vehicle_id": code}, "replacement"), AIMessage(content="已生成报价预览。")])
        with patch.object(vehicle_agent, "tool_model", replacement), \
             patch.object(vehicle_tools, "load_vehicle", wraps=vehicle_tools.load_vehicle) as inventory:
            TaskWorker().run_once()
            inventory.assert_not_called()
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(final["task"]["status"], "completed", final)
        self.assertEqual(final["result"]["plan"]["steps"][0]["agent_id"], completed_agent_id)
        self.assertEqual([step["status"] for step in final["result"]["plan"]["steps"]], ["completed", "completed"])
        with SessionFactory() as session:
            runs = session.scalars(select(AgentRunRecord).where(AgentRunRecord.turn_id == UUID(task["run_id"]))).all()
            self.assertEqual(sum(record.model_calls for record in runs), 7)

    def test_worker_restart_after_plan_checkpoint_does_not_apply_command_twice(self):
        from tests.test_planning import plan_call, tool_call
        from src.services import plan_actions
        from src.database.models import PlanCommandRecord
        from src.agent import plan_control

        code = self.plan_vehicle()
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[
            plan_call("inventory_match"), AIMessage(content="请明确车辆编号。")])):
            task = self.submit(skill_id="deal_review")
            TaskWorker().run_once()
        action, _ = self.post_plan_action(task, "revise", f"改为给 {code} 生成报价预览。")
        original_finish = plan_actions.finish
        failed_once = False

        def fail_ack(command_id, status, **kwargs):
            nonlocal failed_once
            if status == "applied" and not failed_once:
                failed_once = True
                raise RuntimeError("simulated command acknowledgement failure")
            return original_finish(command_id, status, **kwargs)

        model = FakeMessagesListChatModel(responses=[plan_call("quote_assist"),
            tool_call("create_quote_draft", {"vehicle_id": code}, "after-restart"), AIMessage(content="已生成报价预览。")])
        with patch.object(vehicle_agent, "tool_model", model), \
             patch.object(plan_control, "generate_plan", wraps=plan_control.generate_plan) as planner:
            with patch.object(plan_actions, "finish", side_effect=fail_ack):
                TaskWorker().run_once()
            failed = self.get_task(task["task_id"])
            self.assertEqual(failed["status"], "failed")
            # 模拟进程退出后、租约恢复完成时尚未确认的指令状态。
            with SessionFactory.begin() as session:
                command = session.get(PlanCommandRecord, UUID(action["command_id"]))
                command.status, command.finished_at = "queued", None
                session.get(AgentTaskRecord, UUID(task["task_id"])).status = "interrupted"
            self.assertTrue(TaskWorker().run_once())
            planner.assert_called_once()
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(final["task"]["status"], "completed", final)
        self.assertEqual(final["result"]["plan"]["revision"], 2)
        self.assertEqual(final["result"]["quote_draft"]["vehicle_id"], code)

    def test_queued_plan_action_can_be_cancelled_without_being_scheduled_again(self):
        from tests.test_planning import plan_call

        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[
            plan_call("inventory_match"), AIMessage(content="请补充车辆编号。")])):
            task = self.submit(skill_id="deal_review")
            TaskWorker().run_once()
        action, _ = self.post_plan_action(task, "continue", "先停止这次续接。")
        cancelled = self.client.post(f"/api/tasks/{task['task_id']}/cancel", headers=self.headers["owner"])
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        with patch.object(vehicle_agent, "tool_model") as model:
            self.assertFalse(TaskWorker().run_once())
            model.invoke.assert_not_called()
        response = self.client.get(f"/api/conversations/{task['thread_id']}/plan/actions/{action['command_id']}", headers=self.headers["owner"])
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(response.json()["error_code"], "task_cancelled")

    def test_user_can_revise_remaining_goal_and_candidate(self):
        from tests.test_planning import plan_call, tool_call

        code = self.plan_vehicle()
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[
            plan_call("inventory_match"), AIMessage(content="请明确预算。")])):
            task = self.submit(skill_id="deal_review")
            TaskWorker().run_once()
        self.post_plan_action(task, "revise", f"目标改为直接查看 {code} 的报价预览，无需提交审批。")
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[plan_call("quote_assist"),
            tool_call("create_quote_draft", {"vehicle_id": code}, "revised-quote"), AIMessage(content="已按新目标生成报价预览。")])):
            self.assertTrue(TaskWorker().run_once())
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(final["task"]["status"], "completed", final)
        self.assertEqual([step["skill_id"] for step in final["result"]["plan"]["steps"]], ["quote_assist"])
        self.assertEqual(final["result"]["quote_draft"]["vehicle_id"], code)

    def test_planned_task_resumes_current_step_without_replanning_or_replaying_completed_steps(self):
        from tests.test_planning import plan_call, tool_call
        from src.agent.tools import vehicle_tools, quote_tools

        code = self.plan_vehicle()
        model = FailOnceInQuoteStep(responses=[plan_call("inventory_match", "quote_assist"),
            tool_call("get_vehicle", {"vehicle_id": code}, "inventory"), AIMessage(content="已查到车辆，售价86000元。"),
            tool_call("create_quote_draft", {"vehicle_id": code}, "quote"), AIMessage(content="报价预览为86000元。")])
        with patch.object(vehicle_agent, "tool_model", model), \
             patch.object(vehicle_agent, "generate_plan", wraps=vehicle_agent.generate_plan) as planner, \
             patch.object(vehicle_tools, "load_vehicle", wraps=vehicle_tools.load_vehicle) as inventory_read, \
             patch.object(quote_tools, "load_vehicle", wraps=quote_tools.load_vehicle) as quote_read:
            task = self.submit(skill_id="deal_review")
            self.assertTrue(TaskWorker().run_once())
            failed = self.get_task(task["task_id"])
            self.assertEqual(failed["status"], "failed")
            progress = self.client.get(f"/api/conversations/{task['thread_id']}/plan", headers=self.headers["owner"])
            self.assertEqual(progress.status_code, 200, progress.text)
            self.assertEqual(progress.json()["status"], "failed")
            self.assertEqual([step["status"] for step in progress.json()["steps"]], ["completed", "running"])
            self.assertNotIn("snapshot", progress.text)
            self.assertEqual(self.client.get(f"/api/conversations/{task['thread_id']}/plan", headers=self.headers["other"]).status_code, 404)
            resumed = self.client.post(f"/api/runs/{failed['run_id']}/resume", headers=self.headers["owner"])
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(resumed.json()["plan"]["status"], "completed")
            planner.assert_called_once()
            inventory_read.assert_called_once()
            quote_read.assert_called_once()
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(final["task"]["status"], "completed")
        self.assertNotEqual(final["task"]["run_id"], task["run_id"])
        self.assertEqual(final["result"]["plan"]["steps"][1]["result"]["quote_draft"]["quoted_price_cny"], 86000)

    def test_running_plan_can_be_observed_and_cancelled_without_starting_later_tools(self):
        from tests.test_planning import plan_call, tool_call
        from src.agent.tools import quote_tools

        code = self.plan_vehicle()
        model = BlockingQuoteStep(responses=[plan_call("inventory_match", "quote_assist"),
            tool_call("get_vehicle", {"vehicle_id": code}, "inventory"), AIMessage(content="已查询候选车辆。"),
            tool_call("create_quote_draft", {"vehicle_id": code}, "must-not-run")])
        with patch.object(vehicle_agent, "tool_model", model), patch.object(quote_tools, "load_vehicle") as quote_read, \
             ThreadPoolExecutor(max_workers=1) as pool:
            task = self.submit(skill_id="deal_review")
            future = pool.submit(TaskWorker().run_once)
            try:
                self.assertTrue(model._entered.wait(4))
                progress = self.client.get(f"/api/conversations/{task['thread_id']}/plan", headers=self.headers["owner"])
                self.assertEqual(progress.status_code, 200, progress.text)
                self.assertEqual([step["status"] for step in progress.json()["steps"]], ["completed", "running"])
                cancelled = self.client.post(f"/api/tasks/{task['task_id']}/cancel", headers=self.headers["owner"])
                self.assertEqual(cancelled.status_code, 202, cancelled.text)
            finally:
                model._release.set()
            self.assertTrue(future.result(timeout=5))
            quote_read.assert_not_called()
        final = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(final["task"]["status"], "cancelled")
        self.assertEqual(final["result"]["plan"]["status"], "cancelled")
        self.assertEqual([step["status"] for step in final["result"]["plan"]["steps"]], ["completed", "cancelled"])
        with open_checkpointer() as saver:
            state = vehicle_agent.build_vehicle_agent(saver).get_state({"configurable": {"thread_id": task["thread_id"]}})
        self.assertEqual(state.next, ())

    def test_planned_step_budget_is_persistent_across_resume_and_config_relaxation(self):
        from tests.test_planning import plan_call, tool_call
        from src.agent import planning

        code = self.plan_vehicle()
        model = FakeMessagesListChatModel(responses=[plan_call("inventory_match"),
            tool_call("get_vehicle", {"vehicle_id": code}, "inventory"), AIMessage(content="不应执行第二次模型调用。")])
        restricted = self.settings.model_copy(update={"max_model_calls": 1})
        with patch.object(planning, "get_settings", return_value=restricted), patch.object(vehicle_agent, "tool_model", model):
            task = self.submit(skill_id="deal_review")
            self.assertTrue(TaskWorker().run_once())
        failed = self.get_task(task["task_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "execution_budget_exceeded")
        with patch.object(vehicle_agent, "tool_model") as unused:
            resumed = self.client.post(f"/api/runs/{failed['run_id']}/resume", headers=self.headers["owner"])
        self.assertEqual(resumed.status_code, 422, resumed.text)
        self.assertEqual((resumed.json()["detail"]["limit"], resumed.json()["detail"]["used"]), (1, 1))
        unused.invoke.assert_not_called()

    def test_submission_is_immediate_results_and_events_are_durable_and_private(self):
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.return_value = AIMessage(content="PRIVATE_TASK_RESULT")
            task = self.submit()
            model.invoke.assert_not_called()
            self.assertEqual(task["status"], "queued")
            self.assertTrue(TaskWorker().run_once())
        task_id = task["task_id"]
        final = self.get_task(task_id)
        self.assertEqual(final["status"], "completed")
        result = self.client.get(f"/api/tasks/{task_id}/result", headers=self.headers["owner"])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["result"]["reply"], "PRIVATE_TASK_RESULT")
        self.assertNotIn("messages", result.json()["result"])
        page = self.client.get(f"/api/tasks/{task_id}/events", headers=self.headers["owner"]).json()
        self.assertEqual([e["sequence"] for e in page["events"]], list(range(1, len(page["events"]) + 1)))
        self.assertEqual(page["events"][-1]["event"], "task.completed")
        self.assertNotIn("PRIVATE_TASK", str(page))
        after = page["events"][1]["sequence"]
        streamed = self.client.get(f"/api/tasks/{task_id}/events/stream", headers={**self.headers["owner"], "Last-Event-ID": str(after)})
        self.assertEqual(streamed.status_code, 200, streamed.text)
        self.assertIn("text/event-stream", streamed.headers["content-type"])
        self.assertNotIn(f"id: {after}\n", streamed.text)
        self.assertIn("event: task.completed", streamed.text)
        for path in (f"/api/tasks/{task_id}", f"/api/tasks/{task_id}/result", f"/api/tasks/{task_id}/events",
                     f"/api/tasks/{task_id}/events/stream"):
            self.assertEqual(self.client.get(path, headers=self.headers["other"]).status_code, 404)
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)
        self.assertEqual(self.client.get("/api/tasks", headers=self.headers["other"]).json(), [])

    def test_forbidden_tool_batch_is_rejected_and_visible_in_task_events(self):
        from src.agent.tools import vehicle_tools

        model = FakeMessagesListChatModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "create_quote_draft", "args": {"vehicle_id": "PRIVATE-BLOCKED-CAR"},
            "id": "blocked-quote", "type": "tool_call",
        }, {
            "name": "get_vehicle", "args": {"vehicle_id": "PRIVATE-BLOCKED-CAR"},
            "id": "blocked-lookup", "type": "tool_call",
        }]), AIMessage(content="当前技能无法生成报价，请切换报价技能。")])
        with patch.object(vehicle_agent, "tool_model", model), patch.object(vehicle_tools, "SessionFactory") as factory:
            task = self.submit(skill_id="inventory_match")
            self.assertTrue(TaskWorker().run_once())
            factory.assert_not_called()
        self.assertEqual(self.get_task(task["task_id"])["status"], "completed")
        response = self.client.get(f"/api/tasks/{task['task_id']}/events", headers=self.headers["owner"])
        self.assertEqual(response.status_code, 200, response.text)
        events = response.json()["events"]
        rejection = next(event for event in events if event["event"] == "tool.batch.rejected")
        self.assertEqual(rejection["data"]["error_code"], "tool_permission_denied")
        self.assertEqual(rejection["data"]["tool_call_count"], 2)
        self.assertNotIn("tool.start", [event["event"] for event in events])
        self.assertNotIn("PRIVATE-BLOCKED", response.text)

    def test_queued_cancel_and_active_conversation_guard(self):
        task = self.submit()
        thread = task["thread_id"]
        duplicate = self.client.post("/api/tasks", json={"thread_id": thread, "message": "重复"}, headers=self.headers["owner"])
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        with patch.object(vehicle_agent, "tool_model") as model:
            synchronous = self.client.post("/api/chat", json={"thread_id": thread, "message": "插入问题"}, headers=self.headers["owner"])
            self.assertEqual(synchronous.status_code, 409, synchronous.text)
            self.assertEqual(self.client.delete(f"/api/conversations/{thread}", headers=self.headers["owner"]).status_code, 409)
            cancelled = self.client.post(f"/api/tasks/{task['task_id']}/cancel", headers=self.headers["owner"])
            self.assertEqual(cancelled.status_code, 202, cancelled.text)
            self.assertEqual(cancelled.json()["status"], "cancelled")
            self.assertFalse(TaskWorker().run_once())
            model.invoke.assert_not_called()
        next_task = self.submit(thread_id=thread)
        self.assertEqual(next_task["status"], "queued")

    def test_running_task_exposes_live_progress_and_supports_cancellation(self):
        task = self.submit()
        model = BlockingModel(responses=[AIMessage(content="不应作为成功结果返回")])
        with patch.object(vehicle_agent, "tool_model", model), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(TaskWorker().run_once)
            try:
                self.assertTrue(model._entered.wait(5))
                page = self.client.get(f"/api/tasks/{task['task_id']}/events", headers=self.headers["owner"]).json()
                self.assertIn("model.start", [event["event"] for event in page["events"]])
                self.assertEqual(self.get_task(task["task_id"])["status"], "running")
                response = self.client.post(f"/api/tasks/{task['task_id']}/cancel", headers=self.headers["owner"])
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["status"], "cancel_requested")
            finally:
                model._release.set()
            future.result(timeout=5)
        self.assertEqual(self.get_task(task["task_id"])["status"], "cancelled")
        result = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()["result"]
        self.assertEqual(result["status"], "cancelled")

    def test_two_workers_cannot_claim_same_task(self):
        task = self.submit()
        with patch.object(vehicle_agent, "tool_model") as model, ThreadPoolExecutor(max_workers=2) as pool:
            model.invoke.return_value = AIMessage(content="只执行一次")
            futures = [pool.submit(TaskWorker().run_once) for _ in range(2)]
            self.assertEqual(sorted(f.result(timeout=8) for f in futures), [False, True])
            self.assertEqual(model.invoke.call_count, 1)
        self.assertEqual(self.get_task(task["task_id"])["status"], "completed")

    def test_failed_task_can_resume_through_existing_run_interface(self):
        task = self.submit()
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = RuntimeError("PRIVATE_MODEL_FAILURE")
            TaskWorker().run_once()
        failed = self.get_task(task["task_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("PRIVATE", str(failed))
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[AIMessage(content="恢复后的结果")])):
            response = self.client.post(f"/api/runs/{failed['run_id']}/resume", headers=self.headers["owner"])
        self.assertEqual(response.status_code, 200, response.text)
        final = self.get_task(task["task_id"])
        self.assertEqual(final["status"], "completed")
        self.assertNotEqual(final["run_id"], failed["run_id"])
        result = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["owner"]).json()["result"]
        self.assertEqual(result["reply"], "恢复后的结果")

    def test_expired_claim_is_interrupted_and_completed_checkpoint_is_recovered_without_replay(self):
        unfinished = self.submit()
        self.assertIsNotNone(tasks.claim_next())
        with SessionFactory.begin() as session:
            record = session.get(AgentTaskRecord, UUID(unfinished["task_id"]))
            record.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(tasks.recover_expired(), 1)
        self.assertEqual(self.get_task(unfinished["task_id"])["status"], "interrupted")
        completed = self.submit()
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.return_value = AIMessage(content="已保存的最终结果")
            TaskWorker().run_once()
        with SessionFactory.begin() as session:
            record = session.get(AgentTaskRecord, UUID(completed["task_id"]))
            record.status, record.result = "running", None
            record.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            run = session.get(AgentRunRecord, UUID(completed["run_id"]))
            run.status = "running"
        with patch.object(vehicle_agent, "tool_model") as model:
            self.assertEqual(tasks.recover_expired(), 1)
            model.invoke.assert_not_called()
        final = self.client.get(f"/api/tasks/{completed['task_id']}/result", headers=self.headers["owner"]).json()
        self.assertEqual(final["task"]["status"], "completed")
        self.assertEqual(final["result"]["reply"], "已保存的最终结果")

    def test_application_lifespan_starts_background_worker(self):
        with patch.object(vehicle_agent, "tool_model", FakeMessagesListChatModel(responses=[AIMessage(content="后台执行完成")])):
            with TestClient(app) as running_client:
                response = running_client.post("/api/tasks", json={"message": "后台问题"}, headers=self.headers["owner"])
                self.assertEqual(response.status_code, 202, response.text)
                task = response.json()
                self.threads.add(task["thread_id"])
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    result = self.get_task(task["task_id"])
                    if result["status"] == "completed":
                        break
                    time.sleep(0.05)
                self.assertEqual(result["status"], "completed", result)


if __name__ == "__main__":
    unittest.main()
