from dataclasses import replace
import json
from unittest.mock import Mock, patch
import unittest
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from src.agent.config import get_settings
from src.agent.context import create_context
from src.agent.conversation_context import estimate_tokens, prepare_context
from src.agent.planning import generate_plan
from src.agent.skills import get_skill_registry
from src.agent import vehicle_agent
from src.domain.conversation_context import ContextCapacityExceeded


def historical_messages(prefix="history", rounds=35):
    messages = [HumanMessage(content="我的预算十万元，用途是日常通勤，偏好省油。", id=f"{prefix}-goal"),
        AIMessage(content="", id=f"{prefix}-search", tool_calls=[{
            "name": "search_vehicles", "args": {"max_price_cny": 100000}, "id": f"{prefix}-tool"}]),
        ToolMessage(content=json.dumps({"vehicles": [
            {"vehicle_id": "CTX-A", "price_cny": 80000}, {"vehicle_id": "CTX-B", "price_cny": 90000}],
            "has_more": False}), name="search_vehicles", tool_call_id=f"{prefix}-tool", id=f"{prefix}-result"),
        AIMessage(content="可能更适合第二辆，车况仍需检查。", id=f"{prefix}-answer")]
    for index in range(rounds):
        messages.extend([HumanMessage(content=f"讨论第 {index} 轮：" + "补充说明。" * 75, id=f"{prefix}-u{index}"),
                         AIMessage(content="这些是历史讨论，具体资料需要查询。" * 30, id=f"{prefix}-a{index}")])
    return messages


class ConversationContextTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings().model_copy(update={"context_window_tokens": 16000,
            "context_output_tokens": 1000, "context_summary_tokens": 4000, "context_recent_turns": 2})

    def prepare(self, state, prompt="服务端规则", tools=None, **kwargs):
        return prepare_context(state, prompt, tools or [], self.settings, **kwargs)

    def test_budgeted_summary_keeps_sources_conditions_selection_order_and_original_history(self):
        raw = historical_messages() + [HumanMessage(content="第二辆现在多少钱？", id="current")]
        prepared = self.prepare({"messages": raw})
        digest = prepared.digest
        self.assertGreater(digest.covered_messages, 0)
        self.assertIn("十万元", digest.user_conditions["budget"]["user_excerpt"]["text"])
        self.assertEqual(digest.user_conditions["budget"]["confirmation"], "historical_statement_only")
        self.assertEqual(digest.vehicle_selections[0]["vehicles"], [
            {"position": 1, "vehicle_id": "CTX-A"}, {"position": 2, "vehicle_id": "CTX-B"}])
        self.assertNotIn("price_cny", json.dumps(digest.model_dump()))
        self.assertLessEqual(prepared.usage.input_estimated_tokens, prepared.usage.input_limit)
        self.assertGreater(prepared.usage.original_estimated_tokens, prepared.usage.input_limit)
        self.assertIs(prepared.messages[-1], raw[-1])
        self.assertEqual(len(raw), 75)
        self.assertIn("90000", raw[2].content)
        self.assertIn("assistant_excerpt", prepared.messages[0].content)

    def test_incremental_summary_is_idempotent_and_newer_user_conditions_replace_old_excerpts(self):
        raw = historical_messages() + [HumanMessage(content="继续", id="current")]
        first = self.prepare({"messages": raw})
        repeated = self.prepare({"messages": raw, **first.state_update()})
        self.assertEqual(first.digest, repeated.digest)
        raw += [AIMessage(content="继续处理", id="current-answer"),
                HumanMessage(content="预算改成十二万元", id="new-budget"), AIMessage(content="收到", id="new-budget-answer")]
        raw += historical_messages("additional", rounds=10)[4:]
        raw += [HumanMessage(content="继续筛选", id="latest")]
        next_input = self.prepare({"messages": raw, **first.state_update()})
        self.assertGreater(next_input.digest.covered_messages, first.digest.covered_messages)
        self.assertIn("十二万元", next_input.digest.user_conditions["budget"]["user_excerpt"]["text"])
        ids = [item["source_message_id"] for item in next_input.digest.turns]
        self.assertEqual(len(ids), len(set(ids)))

    def test_current_tool_batch_and_report_guard_are_preserved_as_a_whole(self):
        current = [HumanMessage(content="读取报告", id="current"),
            AIMessage(content="", id="read", tool_calls=[{"name": "read_vehicle_report", "args": {}, "id": "r1"},
                                                        {"name": "read_vehicle_report", "args": {}, "id": "r2"}]),
            ToolMessage(content="报告原文1", name="read_vehicle_report", tool_call_id="r1", id="tool1"),
            ToolMessage(content="报告原文2", name="read_vehicle_report", tool_call_id="r2", id="tool2"),
            AIMessage(content="正在整理", id="guard-ai"), SystemMessage(content="必须结构化提交", id="guard")]
        prepared = self.prepare({"messages": historical_messages() + current}, preserve_system=True)
        self.assertEqual(prepared.messages[-len(current):], current)
        for oversized in ([HumanMessage(content="x" * 18000, id="large")],
                          [current[0], current[1], current[2], current[3].model_copy(update={"content": "x" * 18000})]):
            with self.subTest(kind=type(oversized[-1]).__name__), self.assertRaises(ContextCapacityExceeded):
                self.prepare({"messages": oversized})
        with self.assertRaises(ContextCapacityExceeded):
            self.prepare({"messages": [HumanMessage(content="hello")]}, tools=[{"schema": "x" * 18000}])

    def test_orphaned_tool_results_are_rejected_and_foreign_cursor_is_not_reused(self):
        with self.assertRaises(ContextCapacityExceeded):
            self.prepare({"messages": [HumanMessage(content="查车"), ToolMessage(content="x", tool_call_id="orphan")]})
        first = self.prepare({"messages": historical_messages() + [HumanMessage(content="继续", id="current")]})
        other = self.prepare({"messages": [HumanMessage(content="新对话", id="other")], **first.state_update()})
        self.assertEqual(other.digest.covered_messages, 0)
        self.assertNotIn("十万元", str(other.messages))

    def context(self, skill_id="inventory_match"):
        control = Mock()
        control.timeout.return_value = 1
        control.step_id = None
        return replace(create_context(uuid4(), "manager", uuid4(), uuid4(), uuid4(),
            get_skill_registry().get(skill_id, "manager")), control=control)

    def test_compaction_is_checkpointed_before_failure_and_survives_graph_rebuild(self):
        saver, context = InMemorySaver(), self.context()
        config = {"configurable": {"thread_id": str(context.thread_id)}}
        raw = historical_messages()
        with patch.object(vehicle_agent, "get_settings", return_value=self.settings), \
             patch("src.services.memories.confirmed_context", return_value=""), \
             patch.object(vehicle_agent, "tool_model") as model:
            graph = vehicle_agent.build_vehicle_agent(saver, context=context)
            graph.update_state(config, {"messages": raw, "plan": None}, as_node="finish")
            model.invoke.side_effect = RuntimeError("model stopped")
            with self.assertRaises(RuntimeError):
                graph.invoke({"messages": [HumanMessage(content="第二辆呢", id="current")]}, config)
            saved = graph.get_state(config)
            self.assertEqual(saved.next, ("call_model",))
            self.assertGreater(saved.values["context_digest"]["covered_messages"], 0)
            model.invoke.side_effect = None
            model.invoke.return_value = AIMessage(content="需要重新查询第二辆的价格。")
            rebuilt = vehicle_agent.build_vehicle_agent(saver, context=context)
            result = rebuilt.invoke(None, config)
            self.assertEqual(result["context_digest"], saved.values["context_digest"])
            self.assertEqual(len(result["messages"]), len(raw) + 2)
            self.assertEqual(model.invoke.call_args.kwargs["max_tokens"], 1000)

    def test_planner_uses_bounded_history_and_replan_does_not_overwrite_conversation_digest(self):
        context, model = self.context("deal_review"), Mock()
        model.invoke.return_value = AIMessage(content="", tool_calls=[{"id": "p1", "name": "submit_task_plan",
            "args": {"steps": [{"skill_id": "inventory_match", "goal": "按预算查询"}]}}])
        state = {"messages": historical_messages() + [HumanMessage(content="按我的预算找车", id="current")]}
        with patch("src.agent.planning.get_settings", return_value=self.settings), \
             patch("src.services.memories.confirmed_context", return_value=""):
            result = generate_plan(state, {}, context, model)
            self.assertGreater(result["context_digest"]["covered_messages"], 0)
            self.assertIn("conversation_history_data", str(model.invoke.call_args.args[0]))
            self.assertLessEqual(estimate_tokens(model.invoke.call_args.args[0], model.invoke.call_args.kwargs["tools"]),
                                 self.settings.context_window_tokens - self.settings.context_output_tokens)
            replanned = generate_plan({**state, **result, "messages": [*state["messages"], *result["messages"]]},
                                      {}, context, model, previous=result["plan"])
            self.assertNotIn("context_digest", replanned)


if __name__ == "__main__":
    unittest.main()
