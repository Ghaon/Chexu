from contextlib import nullcontext
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from src.agent import vehicle_agent
from src.agent.context import create_context
from src.agent.skills import get_skill_registry, skill_digest
from src.agent.report_analysis import collect_report_evidence, validate_report_analysis
from src.api_view.app import app
from src.domain.report_analysis import ReportAnalysisError
from src.domain.reports import ParsedReport, ReportChunk, VehicleReport
from src.services import reports
from tests.support import install_test_identity


def read_message(report_id, *, vehicle_id="REPORT-001", chunk=1, total=1, truncated=False, text=None):
    return ToolMessage(content=json.dumps({
        "report_id": str(report_id), "vehicle_id": vehicle_id, "filename": "检测报告.txt", "sha256": "a" * 64,
        "parser": "utf8-text", "provider": "local_mcp", "untrusted_document": True,
        "chunks": [{"number": chunk, "text": text or "左前翼子板补漆记录；发动机检查未提供。",
                    "citation": f"[报告 {report_id}#片段{chunk}]"}],
        "total_chunks": total, "has_more": chunk < total, "truncated": truncated,
    }, ensure_ascii=False), name="read_vehicle_report", tool_call_id=f"read-{chunk}")


def analysis_input(report_id, *, vehicle_id="REPORT-001", quote="左前翼子板补漆记录", chunk=1):
    return {"vehicle_id": vehicle_id, "facts": [{"statement": "报告记载左前翼子板补漆。", "evidence": [{
        "report_id": str(report_id), "chunk": chunk, "quote": quote,
    }]}], "unknowns": [], "follow_ups": []}


class ReportAnalysisValidationTests(unittest.TestCase):
    def setUp(self):
        self.report_id = uuid4()
        self.messages = [HumanMessage(content="解读检测报告"), read_message(self.report_id)]

    def test_valid_references_are_enriched_by_server(self):
        result = validate_report_analysis(analysis_input(self.report_id), self.messages)
        reference = result.facts[0].evidence[0]
        self.assertEqual(reference.citation, f"[报告 {self.report_id}#片段1]")
        self.assertEqual(reference.filename, "检测报告.txt")
        self.assertEqual(reference.sha256, "a" * 64)
        self.assertTrue(result.requires_human_review)

    def test_unknown_reports_unread_chunks_and_modified_quotes_are_rejected(self):
        for arguments in (analysis_input(uuid4()), analysis_input(self.report_id, chunk=2),
                          analysis_input(self.report_id, quote="发动机检查正常")):
            with self.subTest(arguments=arguments), self.assertRaises(ReportAnalysisError):
                validate_report_analysis(arguments, self.messages)

    def test_other_vehicle_and_previous_turn_cannot_supply_evidence(self):
        with self.assertRaisesRegex(ReportAnalysisError, "其他车辆"):
            validate_report_analysis(analysis_input(self.report_id, vehicle_id="OTHER-001"), self.messages)
        messages = [*self.messages, HumanMessage(content="这是新的问题")]
        with self.assertRaisesRegex(ReportAnalysisError, "尚未成功读取"):
            validate_report_analysis(analysis_input(self.report_id), messages)

    def test_coverage_tracks_all_read_chunks_instead_of_last_has_more_flag(self):
        messages = [HumanMessage(content="解读"), read_message(self.report_id, chunk=1, total=3),
                    read_message(self.report_id, chunk=3, total=3)]
        result = validate_report_analysis(analysis_input(self.report_id), messages)
        self.assertEqual(result.coverage[0].read_chunks, [1, 3])
        self.assertEqual(result.coverage[0].unread_chunks, 1)
        self.assertFalse(result.coverage[0].extracted_text_complete)
        self.assertTrue(any("尚未读取" in text for text in result.limitations))

    def test_truncation_and_schema_errors_cannot_claim_complete_analysis(self):
        messages = [HumanMessage(content="解读"), read_message(self.report_id, truncated=True)]
        result = validate_report_analysis(analysis_input(self.report_id), messages)
        self.assertFalse(result.coverage[0].extracted_text_complete)
        self.assertTrue(any("长度上限" in text for text in result.limitations))
        for arguments in ({"vehicle_id": "REPORT-001"}, analysis_input(self.report_id) | {"requires_human_review": False}):
            with self.subTest(arguments=arguments), self.assertRaises(ReportAnalysisError):
                validate_report_analysis(arguments, self.messages)

    def test_inconsistent_citations_or_content_are_rejected(self):
        inconsistent = read_message(self.report_id, text="片段内容被替换。")
        with self.assertRaisesRegex(ReportAnalysisError, "原文不一致"):
            collect_report_evidence([*self.messages, inconsistent])


class ReportAnalysisGraphTests(unittest.TestCase):
    def setUp(self):
        install_test_identity(self)
        self.memory = InMemorySaver()
        patcher = patch("src.agent.runtime.open_checkpointer", side_effect=lambda thread_id: nullcontext(self.memory))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.report_id = uuid4()
        self.raw = "左前翼子板补漆记录；发动机检查未提供。"
        self.metadata = VehicleReport(report_id=self.report_id, vehicle_id="REPORT-001", filename="检测报告.txt",
            suffix=".txt", size_bytes=len(self.raw.encode()), sha256="a" * 64,
            uploaded_by_employee_id=uuid4(), created_at=datetime.now(timezone.utc))
        self.parsed = ParsedReport(report_id=self.report_id, sha256="a" * 64, parser="utf8-text", chunks=[
            ReportChunk(number=1, text=self.raw, citation=f"[报告 {self.report_id}#片段1]")], total_chunks=1,
            has_more=False, truncated=False)
        patcher = patch.object(reports, "read_report", return_value=(self.metadata, self.parsed))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def call(self, name, arguments, call_id):
        return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": call_id, "type": "tool_call"}])

    def read(self):
        return self.call("read_vehicle_report", {"vehicle_id": "REPORT-001", "report_id": str(self.report_id)}, "read")

    def finalize(self, **changes):
        arguments = analysis_input(self.report_id)
        if "quote" in changes:
            arguments["facts"][0]["evidence"][0]["quote"] = changes["quote"]
        return self.call("finalize_vehicle_report", arguments, "finalize-" + uuid4().hex)

    def post(self):
        return self.client.post("/api/chat", json={"message": "解读 REPORT-001 的检测报告"})

    def test_invalid_quote_is_returned_to_model_for_bounded_repair(self):
        model = FakeMessagesListChatModel(responses=[self.read(), self.finalize(quote="左前翼子板没有补漆"), self.finalize()])
        with patch.object(vehicle_agent, "tool_model", model):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertIsNotNone(result["report_analysis"])
        state = self.memory.get_tuple({"configurable": {"thread_id": result["thread_id"]}})
        messages = state.checkpoint["channel_values"]["messages"]
        self.assertTrue(any(isinstance(m, ToolMessage) and m.name == "finalize_vehicle_report" and m.status == "error" for m in messages))
        self.assertEqual(messages[-1].response_metadata["generated_by"], "verified_report_renderer")

    def test_free_text_cannot_bypass_structured_result(self):
        model = FakeMessagesListChatModel(responses=[self.read(), AIMessage(content="未经验证的车况结论。"), self.finalize()])
        with patch.object(vehicle_agent, "tool_model", model):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(response.json()["report_analysis"])
        self.assertNotIn("未经验证的车况结论", response.json()["reply"])
        state = self.memory.get_tuple({"configurable": {"thread_id": response.json()["thread_id"]}})
        self.assertFalse(any(getattr(m, "content", None) == "未经验证的车况结论。" for m in state.checkpoint["channel_values"]["messages"]))

    def test_submission_before_reading_is_rejected_then_can_continue(self):
        model = FakeMessagesListChatModel(responses=[self.finalize(), self.read(), self.finalize()])
        with patch.object(vehicle_agent, "tool_model", model):
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["report_analysis"]["facts"][0]["evidence"][0]["quote"], "左前翼子板补漆记录")

    def test_repeated_invalid_submissions_stop_at_existing_budget(self):
        model = FakeMessagesListChatModel(responses=[self.read(), *[
            self.finalize(quote="并不存在的报告摘录") for _ in range(3)]])
        with patch.object(vehicle_agent, "tool_model", model):
            response = self.post()
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"]["code"], "execution_budget_exceeded")

    def test_finalization_cannot_be_batched_with_queries(self):
        mixed = AIMessage(content="", tool_calls=[*self.finalize().tool_calls, {
            "name": "get_vehicle", "args": {"vehicle_id": "REPORT-001"}, "id": "must-not-run", "type": "tool_call",
        }])
        model = FakeMessagesListChatModel(responses=[self.read(), mixed, self.finalize()])
        with patch.object(vehicle_agent, "tool_model", model), patch("src.agent.tools.vehicle_tools.SessionFactory") as factory:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        factory.assert_not_called()
        self.assertIsNotNone(response.json()["report_analysis"])

    def test_old_skill_snapshot_keeps_its_original_output_contract(self):
        current = get_skill_registry().get("vehicle_report", "manager")
        tools = tuple(name for name in current.tools if name != "finalize_vehicle_report")
        old = current.model_copy(update={"version": "1.0.0", "tools": tools,
                                          "digest": skill_digest(current.instructions, tools)})
        thread_id, run_id, employee_id = uuid4(), uuid4(), uuid4()
        context = create_context(employee_id, "manager", thread_id, run_id, run_id, old)
        graph = vehicle_agent.build_vehicle_agent(self.memory, context=context)
        model = FakeMessagesListChatModel(responses=[self.read(), AIMessage(content="原契约的文字结果。")])
        with patch.object(vehicle_agent, "tool_model", model):
            result = graph.invoke({"messages": [HumanMessage(content="解读旧任务")], "report_analysis": None},
                                   config={"configurable": {"thread_id": str(thread_id)}})
        self.assertEqual(result["reply"], "原契约的文字结果。")
        self.assertIsNone(result["report_analysis"])


if __name__ == "__main__":
    unittest.main()
