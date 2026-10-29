from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Timer
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import delete

from src.agent import tracing, vehicle_agent
from src.agent.config import PROJECT_ROOT, get_settings
from src.agent.tools.report_tools import read_vehicle_report
from src.agent.tools.vehicle_tools import VehicleToolError
from src.api_view.app import app
from src.database.checkpoints import open_checkpointer
from src.database.connection import SessionFactory, engine
from src.database.models import (AgentRunRecord, AgentTaskRecord, ConversationRecord, EmployeeRecord, LoginAttemptRecord,
                                  VehicleRecord, VehicleReportRecord)
from src.domain.execution import ExecutionCancelled
from src.domain.reports import ReportReadError
from src.mcp.report_client import read_report_via_mcp
from src.reports.storage import report_path
from src.security import hash_password
from src.services import reports
from tests.report_fixtures import docx_bytes, pdf_bytes


class VehicleReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "report-test-password-2026"
        cls.password_hash = hash_password(cls.password)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        cache = PROJECT_ROOT / ".cache/tmp"
        cache.mkdir(parents=True, exist_ok=True)
        temporary = TemporaryDirectory(dir=cache)
        self.root = Path(temporary.name)
        self.assertTrue(self.root.resolve().is_relative_to(cache.resolve()))
        self.addCleanup(temporary.cleanup)
        self.settings = get_settings().model_copy(update={"report_storage_dir": self.root})
        patcher = patch.object(reports, "get_settings", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.people, self.threads = {}, set()
        self.vehicle_id, self.other_vehicle_id = f"RPT-{uuid4().hex[:20]}", f"RPT-{uuid4().hex[:20]}"
        with SessionFactory.begin() as session:
            for key, role in (("sales", "sales"), ("manager", "manager")):
                employee = EmployeeRecord(username=f"report_{uuid4().hex[:20]}", role=role, password_hash=self.password_hash)
                session.add(employee)
                session.flush()
                self.people[key] = (employee.employee_id, employee.username)
            for code in (self.vehicle_id, self.other_vehicle_id):
                session.add(VehicleRecord(vehicle_id=code, title="报告检查车辆", price_cny=86000, mileage_km=52000, is_demo=True))
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
        ids = [employee[0] for employee in self.people.values()]
        with SessionFactory.begin() as session:
            session.execute(delete(AgentTaskRecord).where(AgentTaskRecord.employee_id.in_(ids)))
            session.execute(delete(AgentRunRecord).where(AgentRunRecord.employee_id.in_(ids)))
            session.execute(delete(ConversationRecord).where(ConversationRecord.employee_id.in_(ids)))
            session.execute(delete(VehicleReportRecord).where(VehicleReportRecord.uploaded_by_employee_id.in_(ids)))
            session.execute(delete(VehicleRecord).where(VehicleRecord.vehicle_id.in_([self.vehicle_id, self.other_vehicle_id])))
            session.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id.in_(ids)))
            session.execute(delete(LoginAttemptRecord).where(LoginAttemptRecord.username.in_([p[1] for p in self.people.values()])))

    def upload(self, filename="检测报告.txt", content="左前翼子板补漆记录。发动机检查未提供。".encode("utf-8")):
        return self.client.post(f"/api/vehicles/{self.vehicle_id}/reports", files={"file": (filename, content)},
                                headers=self.headers["sales"])

    def conversation(self):
        response = self.client.post("/api/conversations", json={"title": "车辆资料解读检查"}, headers=self.headers["sales"])
        self.assertEqual(response.status_code, 201, response.text)
        thread = response.json()["thread_id"]
        self.threads.add(thread)
        return thread

    def test_upload_association_deduplication_listing_and_authentication(self):
        uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        metadata = uploaded.json()
        self.assertEqual(metadata["vehicle_id"], self.vehicle_id)
        self.assertEqual(self.upload().json()["report_id"], metadata["report_id"])
        listing = self.client.get(f"/api/vehicles/{self.vehicle_id}/reports", headers=self.headers["manager"])
        self.assertEqual(len(listing.json()["reports"]), 1)
        self.assertNotIn("storage", uploaded.text)
        self.assertNotIn(str(self.root), uploaded.text)
        self.assertEqual(self.client.get(f"/api/reports/{metadata['report_id']}").status_code, 401)
        self.assertEqual(self.client.get(f"/api/reports/{uuid4()}", headers=self.headers["sales"]).status_code, 404)

    def test_agent_reads_report_through_real_local_mcp_and_returns_original_evidence(self):
        raw = "PRIVATE_REPORT_TEXT_2026：左前翼子板补漆记录；发动机检查未提供。"
        uploaded = self.upload(content=raw.encode("utf-8"))
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        report_id = uploaded.json()["report_id"]
        citation = f"[报告 {report_id}#片段1]"
        model = FakeMessagesListChatModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "list_vehicle_reports", "args": {"vehicle_id": self.vehicle_id},
                                               "id": "report-list", "type": "tool_call"}]),
            AIMessage(content="", tool_calls=[{"name": "read_vehicle_report",
                                               "args": {"vehicle_id": self.vehicle_id, "report_id": report_id},
                                               "id": "report-read", "type": "tool_call"}]),
            AIMessage(content="", tool_calls=[{"name": "finalize_vehicle_report", "args": {
                "vehicle_id": self.vehicle_id,
                "facts": [{"statement": "报告记载左前翼子板有补漆记录。", "evidence": [{
                    "report_id": report_id, "chunk": 1, "quote": "左前翼子板补漆记录"}]}],
                "unknowns": [{"statement": "报告未提供发动机检查结果。", "evidence": [{
                    "report_id": report_id, "chunk": 1, "quote": "发动机检查未提供"}]}],
                "follow_ups": [{"statement": "需要人工核实发动机当前情况。", "evidence": [{
                    "report_id": report_id, "chunk": 1, "quote": "发动机检查未提供"}]}],
            }, "id": "report-finalize", "type": "tool_call"}]),
            AIMessage(content="你好。"),
        ])
        thread = self.conversation()
        with patch.object(vehicle_agent, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            response = self.client.post("/api/chat", json={"thread_id": thread,
                "message": f"解读 {self.vehicle_id} 的检测报告"}, headers=self.headers["sales"])
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["skill_id"], "vehicle_report")
            self.assertEqual(body["report_sources"][0]["text"], raw)
            self.assertEqual(body["report_sources"][0]["citation"], citation)
            self.assertIn(citation, body["reply"])
            self.assertEqual(body["report_analysis"]["vehicle_id"], self.vehicle_id)
            self.assertEqual(body["report_analysis"]["facts"][0]["evidence"][0]["quote"], "左前翼子板补漆记录")
            self.assertTrue(body["report_analysis"]["requires_human_review"])
            self.assertTrue(body["report_analysis"]["coverage"][0]["extracted_text_complete"])
            followup = self.client.post("/api/chat", json={"thread_id": thread, "message": "你好"}, headers=self.headers["sales"])
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(followup.json()["report_sources"], [])
        self.assertIsNone(followup.json()["report_analysis"])
        self.assertIn('"provider": "local_mcp"', " ".join(logs.output))
        self.assertNotIn("PRIVATE_REPORT_TEXT_2026", " ".join(logs.output))

    def test_pdf_docx_are_parsed_locally_and_empty_pdf_reports_ocr_limitation(self):
        for filename, content, expected in (
            ("检测资料.pdf", pdf_bytes(), "Panel repaint recorded"),
            ("维保记录.docx", docx_bytes("左前翼子板存在补漆记录，发动机检查未提供。"), "左前翼子板"),
        ):
            with self.subTest(filename=filename):
                uploaded = self.upload(filename, content)
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                report_id = uploaded.json()["report_id"]
                result = self.client.get(f"/api/reports/{report_id}/content", headers=self.headers["sales"])
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["parser"], "markitdown")
                self.assertIn(expected, result.json()["chunks"][0]["text"])
        empty = self.upload("空白检测报告.pdf", pdf_bytes(""))
        self.assertEqual(empty.status_code, 201, empty.text)
        response = self.client.get(f"/api/reports/{empty.json()['report_id']}/content", headers=self.headers["sales"])
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("OCR", response.json()["detail"])

    def test_vehicle_binding_skill_permission_and_content_digest_are_enforced(self):
        uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        report_id = uploaded.json()["report_id"]
        with patch.object(reports, "read_report_via_mcp") as mcp_call:
            with self.assertRaises(VehicleToolError):
                read_vehicle_report.invoke({"vehicle_id": self.other_vehicle_id, "report_id": report_id})
            mcp_call.assert_not_called()
            model = FakeMessagesListChatModel(responses=[AIMessage(content="", tool_calls=[{
                "name": "read_vehicle_report", "args": {"vehicle_id": self.vehicle_id, "report_id": report_id},
                "id": "forbidden-report", "type": "tool_call",
            }]), AIMessage(content="请切换车辆资料解读技能。")])
            thread = self.conversation()
            with patch.object(vehicle_agent, "tool_model", model):
                result = self.client.post("/api/chat", json={"thread_id": thread, "message": "读取资料",
                    "skill_id": "inventory_match"}, headers=self.headers["sales"])
            self.assertEqual(result.status_code, 200, result.text)
            mcp_call.assert_not_called()
        report_path(self.root, UUID(report_id), ".txt").write_text("被替换的内容", encoding="utf-8")
        response = self.client.get(f"/api/reports/{report_id}/content", headers=self.headers["sales"])
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("校验失败", response.json()["detail"])

    def test_invalid_files_size_limits_and_unknown_vehicle_do_not_create_reports(self):
        for name, content, expected in (("../检测.txt", b"text", 422), ("file.pdf", b"not-pdf", 422),
                                         ("file.txt", b"", 422), ("file.exe", b"binary", 422)):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name, content).status_code, expected)
        small = self.settings.model_copy(update={"max_report_bytes": 1024})
        with patch.object(reports, "get_settings", return_value=small):
            self.assertEqual(self.upload("large.txt", b"x" * 1025).status_code, 413)
        response = self.upload("large.txt", b"x" * (get_settings().max_report_bytes + 65537))
        self.assertEqual(response.status_code, 413, response.text)
        unknown = self.client.post("/api/vehicles/NOT-EXIST/reports", files={"file": ("test.txt", b"text")},
                                    headers=self.headers["sales"])
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(list(self.root.glob("*.txt")), [])

    def test_mcp_timeout_and_cancellation_close_local_execution(self):
        report_id = uuid4()
        content = b"report text"
        report_path(self.root, report_id, ".txt").write_bytes(content)
        arguments = dict(root=self.root, report_id=report_id, suffix=".txt", digest=sha256(content).hexdigest(),
                          start_chunk=1, limit=3, max_bytes=1024, max_text_chars=1600)
        started = time.monotonic()
        with self.assertRaises(ReportReadError):
            read_report_via_mcp(**arguments, timeout_seconds=0.5)
        self.assertLess(time.monotonic() - started, 7)
        cancelled = Event()
        timer = Timer(0.1, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(ExecutionCancelled):
                read_report_via_mcp(**arguments, timeout_seconds=20, cancel_check=cancelled.is_set)
        finally:
            timer.cancel()

    def test_planned_task_runs_inventory_report_comparison_and_quote_through_real_mcp(self):
        from src.agent.task_worker import TaskWorker
        from src.services import tasks
        from tests.test_planning import plan_call, tool_call

        other_code = self.other_vehicle_id
        uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        report_id = uploaded.json()["report_id"]
        comparison = AIMessage(content="", tool_calls=[{"name": "get_vehicle", "args": {"vehicle_id": code},
            "id": f"compare-{index}", "type": "tool_call"} for index, code in enumerate((self.vehicle_id, other_code))])
        model = FakeMessagesListChatModel(responses=[
            plan_call("inventory_match", "vehicle_report", "vehicle_compare", "quote_assist"),
            tool_call("get_vehicle", {"vehicle_id": self.vehicle_id}, "inventory"), AIMessage(content="已查询目标车辆。"),
            tool_call("read_vehicle_report", {"vehicle_id": self.vehicle_id, "report_id": report_id}, "read"),
            tool_call("finalize_vehicle_report", {"vehicle_id": self.vehicle_id, "facts": [{
                "statement": "报告记载左前翼子板补漆。", "evidence": [{"report_id": report_id, "chunk": 1,
                "quote": "左前翼子板补漆记录"}]}]}, "finalize"),
            comparison, AIMessage(content="已依据库存资料比较两辆模拟车辆，车况仍需人工复检。"),
            tool_call("create_quote_draft", {"vehicle_id": self.vehicle_id}, "quote"), AIMessage(content="已生成报价预览，未保存确认。")])
        settings = get_settings().model_copy(update={"task_queue_name": f"plan_report_{uuid4().hex}"})
        with patch.object(tasks, "get_settings", return_value=settings), patch.object(vehicle_agent, "tool_model", model):
            response = self.client.post("/api/tasks", json={"message": "查询车辆，解读检测报告，对比后给报价预览。",
                "skill_id": "deal_review"}, headers=self.headers["sales"])
            self.assertEqual(response.status_code, 202, response.text)
            task = response.json()
            self.threads.add(task["thread_id"])
            self.assertTrue(TaskWorker().run_once())
        response = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["sales"])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertEqual(response.json()["task"]["status"], "completed", response.text)
        self.assertEqual([step["status"] for step in result["plan"]["steps"]], ["completed"] * 4)
        self.assertIsNotNone(result["report_analysis"])
        self.assertEqual(result["report_analysis"]["facts"][0]["evidence"][0]["quote"], "左前翼子板补漆记录")
        self.assertIsNotNone(result["quote_draft"])
        self.assertIsNone(result["quote_id"])
        self.assertTrue(all("snapshot" not in step for step in result["plan"]["steps"]))
        events = self.client.get(f"/api/tasks/{task['task_id']}/events", headers=self.headers["sales"]).json()["events"]
        self.assertEqual([event["data"]["skill_id"] for event in events if event["event"] == "plan.step.started"],
            ["inventory_match", "vehicle_report", "vehicle_compare", "quote_assist"])
        self.assertTrue(any(event["data"].get("provider") == "local_mcp" for event in events))

    def test_background_report_analysis_persists_mcp_and_validation_events(self):
        from src.agent.task_worker import TaskWorker
        from src.services import tasks

        uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        report_id = uploaded.json()["report_id"]
        model = FakeMessagesListChatModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "read_vehicle_report", "args": {"vehicle_id": self.vehicle_id, "report_id": report_id},
            "id": "background-report-read", "type": "tool_call",
        }]), AIMessage(content="", tool_calls=[{
            "name": "finalize_vehicle_report", "args": {"vehicle_id": self.vehicle_id, "facts": [{
                "statement": "报告记载左前翼子板有补漆记录。", "evidence": [{
                    "report_id": report_id, "chunk": 1, "quote": "左前翼子板补漆记录"}]}]},
            "id": "background-report-finalize", "type": "tool_call",
        }])])
        settings = get_settings().model_copy(update={"task_queue_name": f"report_{uuid4().hex}"})
        with patch.object(tasks, "get_settings", return_value=settings), patch.object(vehicle_agent, "tool_model", model):
            response = self.client.post("/api/tasks", json={"message": f"解读 {self.vehicle_id} 的检测报告"}, headers=self.headers["sales"])
            self.assertEqual(response.status_code, 202, response.text)
            task = response.json()
            self.threads.add(task["thread_id"])
            self.assertTrue(TaskWorker().run_once())
        result = self.client.get(f"/api/tasks/{task['task_id']}/result", headers=self.headers["sales"])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["task"]["status"], "completed")
        self.assertEqual(result.json()["result"]["report_analysis"]["facts"][0]["evidence"][0]["quote"], "左前翼子板补漆记录")
        events = self.client.get(f"/api/tasks/{task['task_id']}/events", headers=self.headers["sales"]).json()["events"]
        self.assertTrue(any(event["data"].get("provider") == "local_mcp" for event in events))
        self.assertIn("report.analysis.validated", [event["event"] for event in events])


if __name__ == "__main__":
    unittest.main()
