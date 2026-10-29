from contextlib import nullcontext
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from src.agent import vehicle_agent
from src.agent.config import get_settings
from src.agent.planning import result_status
from src.agent.subagents import make_handoff
from src.agent.web_sources import collect_web_sources
from src.api_view.app import app
from src.domain.web_search import WebSearchError
from src.services import web_search
from tests.support import install_test_identity


def source():
    return {"citation": "W-0123456789ab", "title": "公开车型资料", "url": "https://example.com/car",
            "snippet": "年款说明。", "query": "公开车型资料", "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "published_date": None, "source_type": "web_reference"}


class TavilyTransportTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings().model_copy(update={"tavily_api_key": SecretStr("tvly-test-only"),
            "tavily_use_env_proxy": False, "tavily_max_results": 5})
        patcher = patch.object(web_search, "get_settings", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.real_client = httpx.Client

    def transport(self, handler):
        return patch.object(web_search.httpx, "Client", side_effect=lambda **kwargs:
            self.real_client(transport=httpx.MockTransport(handler), **kwargs))

    def test_request_auth_bounds_and_sources(self):
        def respond(request):
            self.assertEqual(str(request.url), web_search.SEARCH_URL)
            self.assertEqual(request.headers["Authorization"], "Bearer tvly-test-only")
            body = json.loads(request.content)
            self.assertEqual(body["time_range"], "month")
            self.assertEqual(body["search_depth"], "basic")
            self.assertFalse(body["include_answer"])
            self.assertFalse(body["include_raw_content"])
            return httpx.Response(200, json={"results": [
                {"title": "资料", "url": "https://example.com/car", "content": "x" * 2000},
                {"url": "https://example.com/car"}, {"url": "javascript:alert(1)"},
                {"url": "http://127.0.0.1/private"}, {"url": "https://user:secret@example.com/"}]})
        with self.transport(respond):
            result = web_search.search("公开车型资料", time_range="month", timeout_ms=2500)
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(len(result["sources"][0]["snippet"]), 1600)
        self.assertEqual(result["sources"][0]["source_type"], "web_reference")
        self.assertIsNone(result["sources"][0]["published_date"])
        self.assertNotIn("tvly-test-only", json.dumps(result))

    def test_missing_key_never_contacts_provider(self):
        self.settings.tavily_api_key = None
        with patch.object(web_search.httpx, "Client") as client, self.assertRaisesRegex(WebSearchError, "尚未配置"):
            web_search.search("公开资料查询")
        client.assert_not_called()

    def test_provider_failures_are_safe_and_are_not_retried(self):
        for status, expected in [(401, "密钥"), (429, "额度"), (432, "额度"), (500, "暂不可用")]:
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(status, text="tvly-test-only internal-provider-details")
            with self.subTest(status=status), self.transport(respond), self.assertRaisesRegex(WebSearchError, expected) as raised:
                web_search.search("公开资料查询")
            self.assertEqual(len(calls), 1)
            self.assertNotIn("tvly-test-only", str(raised.exception))
        def timeout(request):
            raise httpx.ReadTimeout("secret network detail", request=request)
        with self.transport(timeout), self.assertRaisesRegex(WebSearchError, "超时"):
            web_search.search("公开资料查询")

    def test_response_size_and_schema_limits(self):
        for payload in [b"x" * (web_search.MAX_RESPONSE_BYTES + 1), b'{"results": {}}']:
            with self.subTest(size=len(payload)), self.transport(lambda _: httpx.Response(200, content=payload)), self.assertRaises(WebSearchError):
                web_search.search("公开资料查询")


class WebSearchFlowTests(unittest.TestCase):
    def setUp(self):
        install_test_identity(self)
        saver = InMemorySaver()
        patcher = patch("src.agent.runtime.open_checkpointer", side_effect=lambda _: nullcontext(saver))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_tool_result_sources_and_history_survive_api_flow(self):
        data = {"provider": "tavily", "query": "公开车型资料", "sources": [source()]}
        with patch("src.agent.tools.web_tools.search", return_value=data) as search, patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="", tool_calls=[{"name": "search_web", "args": {"query": "公开车型资料"}, "id": "web-1"}]),
                AIMessage(content="找到车型资料 [W-0123456789ab]。")]
            response = self.client.post("/api/chat", json={"message": "请联网查询车型资料"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["skill_id"], "web_research")
        self.assertEqual(result["web_search_status"], "completed")
        self.assertEqual(result["web_sources"][0]["url"], source()["url"])
        self.assertIn("](https://example.com/car)", result["reply"])
        self.assertGreater(search.call_args.kwargs["timeout_ms"], 0)
        self.assertIn("search_web", json.dumps(model.invoke.call_args.kwargs["tools"]))
        with patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.return_value = AIMessage(content="请提供库存查询条件。")
            response = self.client.post("/api/chat", json={"thread_id": result["thread_id"], "message": "查询库存", "skill_id": "inventory_match"})
        self.assertEqual(response.json()["web_sources"], [])
        self.assertIsNone(response.json()["web_search_status"])

    def test_unconfigured_search_cannot_claim_success(self):
        with patch("src.agent.tools.web_tools.search", side_effect=WebSearchError("尚未配置 Tavily 密钥。")), patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [AIMessage(content="", tool_calls=[{"name": "search_web", "args": {"query": "公开车型资料"}, "id": "web-1"}]), AIMessage(content="搜索成功，价格十万元。")]
            result = self.client.post("/api/chat", json={"message": "联网搜索车型"}).json()
        self.assertEqual(result["web_search_status"], "failed")
        self.assertEqual(result["web_sources"], [])
        self.assertIn("尚未配置", result["reply"])
        self.assertNotIn("十万元", result["reply"])

    def test_unrelated_skill_cannot_call_web_tool(self):
        with patch("src.agent.tools.web_tools.search") as search, patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [AIMessage(content="", tool_calls=[{"name": "search_web", "args": {"query": "公开车型资料"}, "id": "web-1"}]), AIMessage(content="当前技能不能联网查询。")]
            response = self.client.post("/api/chat", json={"message": "查询库存", "skill_id": "inventory_match"})
        self.assertEqual(response.status_code, 200)
        search.assert_not_called()

    def test_planner_executes_isolated_web_subagent_and_returns_sources(self):
        data = {"provider": "tavily", "sources": [source()]}
        with patch("src.agent.tools.web_tools.search", return_value=data), patch.object(vehicle_agent, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="", tool_calls=[{"name": "submit_task_plan", "id": "plan-1", "args": {"steps": [{"skill_id": "web_research", "goal": "搜索公开车型资料"}]}}]),
                AIMessage(content="", tool_calls=[{"name": "search_web", "args": {"query": "公开车型资料"}, "id": "web-1"}]),
                AIMessage(content="车型资料 [W-0123456789ab]。")]
            response = self.client.post("/api/chat", json={"message": "搜索公开车型资料", "skill_id": "deal_review"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["plan"]["steps"][0]["status"], "completed")
        self.assertEqual(len(result["web_sources"]), 1)
        self.assertTrue(result["plan"]["steps"][0]["agent_id"])

    def test_sources_are_scoped_and_handoff_retains_reference_kind(self):
        old = ToolMessage(content=json.dumps({"provider": "tavily", "sources": [source()]}), name="search_web", tool_call_id="old")
        messages = [old, HumanMessage(content="新问题"), ToolMessage(content="密钥无效", name="search_web", tool_call_id="new", status="error")]
        sources, status, _ = collect_web_sources(messages)
        self.assertEqual((sources, status), ([], "failed"))
        self.assertEqual(result_status(messages[1:], {"web_sources": sources, "web_search_status": status}, "web_research"), "blocked")
        first = {"id": "s1", "skill_id": "web_research", "result": {"web_sources": [source()]}}
        second = {"id": "s2", "skill_id": "vehicle_compare", "goal": "参考公开资料比较车辆", "depends_on": ["s1"]}
        packet = make_handoff({"steps": [first, second]}, second)
        self.assertEqual(packet["web_sources"][0]["source_type"], "web_reference")
        self.assertIn("不是门店库存事实", packet["warnings"][0])
