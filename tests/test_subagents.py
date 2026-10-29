from contextlib import nullcontext
import json
from threading import Barrier, Lock
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr

from src.agent import vehicle_agent
from src.agent.context import create_context
from src.agent.planning import generate_plan, PlanError
from src.agent.skills import get_skill_registry
from src.agent.tools import quote_tools, vehicle_tools
from src.api_view.app import app
from src.domain.reports import ReportReadError
from src.domain.conversation_context import ContextCapacityExceeded
from src.services import reports
from tests.support import install_test_identity
from tests.test_planning import plan_call, tool_call


class ParallelInventoryModel(FakeMessagesListChatModel):
    vehicle_codes: list[str] = ["PARALLEL-A", "PARALLEL-B"]
    fail_code: str | None = None
    _gate: Barrier = PrivateAttr(default_factory=lambda: Barrier(2))
    _lock: Lock = PrivateAttr(default_factory=Lock)
    _packets: list = PrivateAttr(default_factory=list)
    _failed: bool = PrivateAttr(default=False)
    _planning_calls: int = PrivateAttr(default=0)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        names = {item["function"]["name"] for item in kwargs.get("tools", [])}
        if "submit_task_plan" in names:
            self._planning_calls += 1
            response = tool_call("submit_task_plan", {"steps": [
                {"skill_id": "inventory_match", "goal": f"查询 {code}", "depends_on": [], "inputs": {"vehicle_ids": [code]}}
                for code in self.vehicle_codes]}, "parallel-plan")
        else:
            packet = json.loads(next(item.content for item in messages if isinstance(item, HumanMessage)))
            code = packet["inputs"]["vehicle_ids"][0]
            if not any(isinstance(item, ToolMessage) for item in messages):
                with self._lock:
                    self._packets.append(packet)
                # 两个模型调用都进入后才继续；串行调度会在这里失败。
                self._gate.wait(timeout=4)
                response = tool_call("get_vehicle", {"vehicle_id": code}, f"read-{code}")
            else:
                if code == self.fail_code and not self._failed:
                    self._failed = True
                    raise RuntimeError("private-parallel-agent-failure")
                response = AIMessage(content=f"已查询模拟车辆 {code}。")
        return ChatResult(generations=[ChatGeneration(message=response)])


class SubagentTests(unittest.TestCase):
    def setUp(self):
        install_test_identity(self)
        self.memory = InMemorySaver()
        patcher = patch("src.agent.runtime.open_checkpointer", side_effect=lambda _: nullcontext(self.memory))
        patcher.start()
        self.addCleanup(patcher.stop)

        def load(code, timeout_ms=None):
            return {"vehicle_id": code, "title": "子任务检查车辆", "price_cny": 86000, "mileage_km": 50000, "is_demo": True}

        for module in (vehicle_tools, quote_tools):
            patcher = patch.object(module, "load_vehicle", side_effect=load)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def post(self):
        return self.client.post("/api/chat", json={"skill_id": "deal_review",
            "message": "PRIVATE-PARENT-HISTORY：查询车辆资料，报告读取失败时可以先查询库存。"})

    def test_isolated_contexts_receive_only_structured_dependency_data(self):
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [plan_call("inventory_match", "quote_assist"),
                tool_call("get_vehicle", {"vehicle_id": "ISOLATED-A"}, "read"), AIMessage(content="PRIVATE-INVENTORY-REPLY"),
                tool_call("create_quote_draft", {"vehicle_id": "ISOLATED-A"}, "quote"), AIMessage(content="报价预览已生成。")]
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        quote_messages = model.invoke.call_args_list[3].args[0]
        self.assertFalse(any(isinstance(item, ToolMessage) for item in quote_messages))
        self.assertNotIn("PRIVATE-PARENT-HISTORY", str(quote_messages))
        self.assertNotIn("PRIVATE-INVENTORY-REPLY", str(quote_messages))
        packet = json.loads(next(item.content for item in quote_messages if isinstance(item, HumanMessage)))
        self.assertEqual(packet["vehicles"][0]["vehicle_id"], "ISOLATED-A")
        state = vehicle_agent.build_vehicle_agent(self.memory).get_state({"configurable": {"thread_id": response.json()["thread_id"]}})
        self.assertFalse(any(isinstance(item, ToolMessage) and item.name == "get_vehicle" for item in state.values["messages"]))
        self.assertTrue(all("messages" not in item for item in state.values["child_results"].values()))
        snapshots = [item for item in self.memory.list(None) if item.config["configurable"].get("thread_id") == response.json()["thread_id"]]
        namespaces = {item.config["configurable"].get("checkpoint_ns") for item in snapshots}
        self.assertGreaterEqual(len([name for name in namespaces if name and "delegate:" in name]), 2)

    def test_independent_read_only_agents_actually_run_in_parallel(self):
        model = ParallelInventoryModel(responses=[AIMessage(content="unused")])
        with patch.object(vehicle_agent, "tool_model", model):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()["plan"]
        self.assertEqual(plan["status"], "completed")
        self.assertEqual([step["status"] for step in plan["steps"]], ["completed", "completed"])
        self.assertEqual(len({step["agent_id"] for step in plan["steps"]}), 2)
        self.assertEqual(len(model._packets), 2)
        self.assertTrue(all(packet["vehicles"] == [] for packet in model._packets))

    def test_blocked_report_can_trigger_one_alternative_plan_with_visible_reason(self):
        read = tool_call("read_vehicle_report", {"vehicle_id": "ISOLATED-A", "report_id": str(uuid4())}, "unreadable")
        with patch.object(reports, "read_report", side_effect=ReportReadError("报告无法解析。")), \
             patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [plan_call("vehicle_report"), read, AIMessage(content="该报告读取失败，尚未完成解读。"),
                plan_call("inventory_match"), tool_call("get_vehicle", {"vehicle_id": "ISOLATED-A"}, "fallback"),
                AIMessage(content="已查到车辆库存资料，报告结论仍未知。")]
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["plan"]["replan_count"], 1)
        self.assertEqual(response.json()["plan"]["revision"], 2)
        self.assertIn("报告读取失败", response.json()["reply"])
        self.assertTrue(response.json()["plan"]["adjustments"])

    def test_automatic_replanning_stops_at_configured_limit(self):
        read = tool_call("read_vehicle_report", {"vehicle_id": "ISOLATED-A", "report_id": str(uuid4())}, "unreadable")
        with patch.object(reports, "read_report", side_effect=ReportReadError("报告无法解析。")), \
             patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [plan_call("vehicle_report"), read, AIMessage(content="需要可解析的报告。"),
                                       plan_call("vehicle_report"), read, AIMessage(content="仍然缺少可解析资料。")]
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["plan"]["status"], "needs_input")
        self.assertEqual(response.json()["plan"]["replan_count"], 1)
        self.assertEqual(model.invoke.call_count, 6)

    def test_oversized_planner_context_is_rejected_before_model_call(self):
        context = create_context(uuid4(), "manager", uuid4(), uuid4(), uuid4(), get_skill_registry().get("deal_review", "manager"))
        model = Mock()
        with self.assertRaises(ContextCapacityExceeded):
            generate_plan({"messages": [HumanMessage(content="原任务"), AIMessage(content="可以继续"),
                                        HumanMessage(content="x" * 100001)]}, {}, context, model)
        model.invoke.assert_not_called()
        self.assertEqual(context.budget.model_calls, 0)

    def test_forward_or_cyclic_dependencies_are_rejected(self):
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.return_value = tool_call("submit_task_plan", {"steps": [
                {"skill_id": "inventory_match", "goal": "查车", "depends_on": [2]},
                {"skill_id": "vehicle_compare", "goal": "比较", "depends_on": [1]}]}, "invalid-cycle")
            response = self.post()
        self.assertEqual(response.status_code, 400, response.text)
        model.invoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()
