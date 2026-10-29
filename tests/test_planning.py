from contextlib import nullcontext
import json
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from src.agent import vehicle_agent
from src.agent.context import create_context
from src.agent.planning import generate_plan
from src.agent.skills import get_skill_registry
from src.agent.tools import vehicle_tools, quote_tools
from src.api_view.app import app
from tests.support import install_test_identity


def tool_call(name, arguments, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": call_id, "type": "tool_call"}])


def plan_call(*skills):
    return tool_call("submit_task_plan", {"steps": [{"skill_id": skill, "goal": f"执行 {skill}"} for skill in skills]}, "plan")


class PlanningFlowTests(unittest.TestCase):
    def setUp(self):
        install_test_identity(self)
        memory = InMemorySaver()
        patcher = patch("src.agent.runtime.open_checkpointer", side_effect=lambda _: nullcontext(memory))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.loads = []

        def load(vehicle_id, timeout_ms=None):
            self.loads.append(vehicle_id)
            return {"vehicle_id": vehicle_id, "title": "规划检查车辆", "price_cny": 86000,
                    "mileage_km": 52000, "is_demo": True}

        for module in (vehicle_tools, quote_tools):
            patcher = patch.object(module, "load_vehicle", side_effect=load)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def post(self, message="比较车辆后给出报价预览"):
        return self.client.post("/api/chat", json={"message": message, "skill_id": "deal_review"})

    def test_three_skills_run_in_order_with_step_specific_tool_payloads(self):
        comparison = AIMessage(content="", tool_calls=[
            {"name": "get_vehicle", "args": {"vehicle_id": vehicle}, "id": vehicle, "type": "tool_call"}
            for vehicle in ("PLAN-001", "PLAN-002")])
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [plan_call("inventory_match", "vehicle_compare", "quote_assist"),
                tool_call("get_vehicle", {"vehicle_id": "PLAN-001"}, "inventory"), AIMessage(content="已查询候选车辆。"),
                comparison, AIMessage(content="两辆模拟车辆价格相同。"),
                tool_call("create_quote_draft", {"vehicle_id": "PLAN-001"}, "quote"), AIMessage(content="报价预览为86000元，尚未保存。")]
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["plan"]["status"], "completed")
        self.assertEqual([step["status"] for step in result["plan"]["steps"]], ["completed"] * 3)
        self.assertEqual(result["quote_draft"]["quoted_price_cny"], 86000)
        payloads = [{tool["function"]["name"] for tool in call.kwargs["tools"]} for call in model.invoke.call_args_list]
        self.assertEqual(payloads[0], {"submit_task_plan"})
        self.assertEqual(payloads[1], {"get_vehicle", "search_vehicles"})
        self.assertNotIn("create_quote_draft", payloads[3])
        self.assertIn("create_quote_draft", payloads[5])
        self.assertNotIn("request_quote_approval", payloads[5])
        self.assertNotIn("snapshot", json.dumps(result["plan"]))
        self.assertNotIn("allow_approval", json.dumps(result["plan"]))

    def test_invalid_plans_are_rejected_before_any_business_tool_runs(self):
        for skills in (("unknown_skill",), ("quote_assist", "inventory_match"), ("quote_assist", "quote_assist")):
            with self.subTest(skills=skills), patch.object(vehicle_agent, "tool_model") as model:
                model.invoke.return_value = plan_call(*skills)
                response = self.post()
                self.assertEqual(response.status_code, 400, response.text)
                model.invoke.assert_called_once()
        self.assertEqual(self.loads, [])

    def test_missing_information_stops_the_plan_without_executing_later_steps(self):
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [plan_call("inventory_match", "quote_assist"), AIMessage(content="请先提供预算和车辆编号。")]
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["plan"]["status"], "needs_input")
        self.assertEqual([step["status"] for step in response.json()["plan"]["steps"]], ["needs_input", "skipped"])
        self.assertEqual(model.invoke.call_count, 2)
        self.assertEqual(self.loads, [])

    def test_tool_from_another_step_is_denied_even_when_it_is_in_the_plan(self):
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [plan_call("inventory_match", "quote_assist"),
                tool_call("create_quote_draft", {"vehicle_id": "PLAN-001"}, "early-quote"), AIMessage(content="需要先明确车辆。")]
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["plan"]["status"], "needs_input")
        self.assertEqual(self.loads, [])

    def test_prior_report_guard_is_not_forwarded_to_the_planner(self):
        context = create_context(uuid4(), "manager", uuid4(), uuid4(), uuid4(), get_skill_registry().get("deal_review", "manager"))
        model = Mock()
        model.invoke.return_value = plan_call("inventory_match")
        state = {"messages": [HumanMessage(content="上一轮报告任务"), SystemMessage(content="PRIVATE-OLD-REPORT-GUARD"),
                              HumanMessage(content="本轮查库存") ]}
        generate_plan(state, {}, context, model)
        forwarded = model.invoke.call_args.args[0]
        self.assertEqual(sum(isinstance(message, SystemMessage) for message in forwarded), 1)
        self.assertNotIn("PRIVATE-OLD-REPORT-GUARD", str(forwarded))

    def test_approval_tool_requires_explicit_user_request_not_only_planner_goal(self):
        for message, allowed in (("给出报价建议", False), ("生成报价并提交经理审批", True), ("生成报价，不要提交审批", False)):
            with self.subTest(message=message), patch.object(vehicle_agent, "tool_model") as model:
                model.invoke.side_effect = [plan_call("quote_assist"), AIMessage(content="请提供车辆编号。")]
                response = self.post(message)
                self.assertEqual(response.status_code, 200, response.text)
                names = {item["function"]["name"] for item in model.invoke.call_args_list[1].kwargs["tools"]}
                self.assertEqual("request_quote_approval" in names, allowed)


if __name__ == "__main__":
    unittest.main()
