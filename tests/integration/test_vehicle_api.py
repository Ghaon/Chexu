from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import datetime
import json
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.agent.config import Settings
from src.agent import vehicle_agent as graph_module
from src.agent.tools import vehicle_tools
from src.api_view import vehicles as vehicles_api
from src.api_view.app import app
from src.database.models import VehicleRecord
from tests.support import install_test_identity


class VehicleApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            Settings().database_url.get_secret_value(),
            connect_args={"connect_timeout": 5},
            hide_parameters=True,
        )
        cls.session_factory = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        install_test_identity(self)
        memory = InMemorySaver()
        memory_patch = patch(
            "src.agent.runtime.open_checkpointer",
            side_effect=lambda thread_id: nullcontext(memory),
        )
        memory_patch.start()
        self.addCleanup(memory_patch.stop)
        self.created_codes = set()
        self.payload = {
            "vehicle_id": self.new_code(),
            "title": "接口集成检查车辆",
            "price_cny": 96800,
            "mileage_km": 64000,
        }
        self.factory_patch = patch.object(vehicles_api, "SessionFactory", self.session_factory)
        self.factory_patch.start()
        tool_patch = patch.object(vehicle_tools, "SessionFactory", self.session_factory)
        tool_patch.start()
        self.addCleanup(tool_patch.stop)
        self.client = TestClient(app)

    def tearDown(self):
        try:
            # 接口必须真正提交；检查后只删除本次生成编号对应的记录。
            with self.engine.begin() as connection:
                connection.execute(
                    delete(VehicleRecord).where(VehicleRecord.vehicle_id.in_(self.created_codes))
                )
        finally:
            self.factory_patch.stop()
            self.client.close()

    def new_code(self):
        code = f"API-{uuid4().hex[:20]}"
        self.created_codes.add(code)
        return code

    def test_create_returns_generated_fields_and_commits_to_database(self):
        body = self.payload | {
            "vehicle_id": f"  {self.payload['vehicle_id']}  ",
            "title": f"  {self.payload['title']}  ",
        }
        response = self.client.post("/api/vehicles", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        self.assertEqual(result["vehicle_id"], self.payload["vehicle_id"])
        self.assertEqual(result["title"], self.payload["title"])
        self.assertFalse(result["is_demo"])
        self.assertIsInstance(result["id"], int)
        self.assertIsNotNone(datetime.fromisoformat(result["created_at"]).tzinfo)

        # 独立连接可读到数据，证明请求结束前事务已经提交。
        with self.engine.connect() as connection:
            stored = connection.execute(
                select(VehicleRecord.id, VehicleRecord.price_cny).where(
                    VehicleRecord.vehicle_id == self.payload["vehicle_id"]
                )
            ).one()
        self.assertEqual(stored.id, result["id"])
        self.assertEqual(stored.price_cny, self.payload["price_cny"])

    def test_duplicate_returns_conflict_without_overwriting_existing_vehicle(self):
        first = self.client.post("/api/vehicles", json=self.payload)
        self.assertEqual(first.status_code, 201, first.text)
        duplicate = self.client.post("/api/vehicles", json=self.payload | {"price_cny": 1})
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(duplicate.json()["detail"], "该车辆编号已存在。")
        with self.engine.connect() as connection:
            price = connection.scalar(
                select(VehicleRecord.price_cny).where(
                    VehicleRecord.vehicle_id == self.payload["vehicle_id"]
                )
            )
        self.assertEqual(price, self.payload["price_cny"])

        following = self.client.post(
            "/api/vehicles", json=self.payload | {"vehicle_id": self.new_code()}
        )
        self.assertEqual(following.status_code, 201, following.text)

    def test_invalid_input_is_rejected_before_database_access(self):
        invalid_changes = (
            {"vehicle_id": "   "},
            {"vehicle_id": "X" * 33},
            {"vehicle_id": "X\x00Y"},
            {"title": "\n\t"},
            {"title": "X" * 201},
            {"title": "X\x00Y"},
            {"price_cny": -1},
            {"price_cny": 1.5},
            {"price_cny": True},
            {"price_cny": "96800"},
            {"price_cny": 2**31},
            {"mileage_km": -1},
            {"mileage_km": 2**31},
            {"id": 123},
            {"created_at": "2026-01-01T00:00:00Z"},
            {"is_demo": True},
        )
        with patch.object(vehicles_api, "SessionFactory") as factory:
            for changes in invalid_changes:
                with self.subTest(changes=changes):
                    response = self.client.post("/api/vehicles", json=self.payload | changes)
                    self.assertEqual(response.status_code, 422, response.text)
            missing = self.client.post("/api/vehicles", json={"vehicle_id": self.payload["vehicle_id"]})
            self.assertEqual(missing.status_code, 422)
            factory.begin.assert_not_called()

    def test_concurrent_duplicate_requests_only_create_one_vehicle(self):
        ready = Barrier(2)

        def submit():
            with TestClient(app) as client:
                ready.wait(timeout=10)
                return client.post("/api/vehicles", json=self.payload).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(submit) for _ in range(2)]
            statuses = [request.result(timeout=20) for request in requests]
        self.assertEqual(sorted(statuses), [201, 409])
        with self.engine.connect() as connection:
            ids = connection.scalars(
                select(VehicleRecord.id).where(VehicleRecord.vehicle_id == self.payload["vehicle_id"])
            ).all()
        self.assertEqual(len(ids), 1)

    def test_unavailable_database_returns_safe_error(self):
        with patch.object(vehicles_api, "SessionFactory") as factory:
            factory.begin.side_effect = OperationalError(None, None, RuntimeError("private-db-detail"))
            with self.assertLogs(vehicles_api.logger, level="WARNING"):
                response = self.client.post("/api/vehicles", json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-db-detail", response.text)

    def test_commit_failure_does_not_report_success_or_leave_a_record(self):
        def fail_commit(connection):
            raise OperationalError(None, None, RuntimeError("private-commit-detail"))

        event.listen(self.engine, "commit", fail_commit)
        try:
            with self.assertLogs(vehicles_api.logger, level="WARNING"):
                response = self.client.post("/api/vehicles", json=self.payload)
        finally:
            event.remove(self.engine, "commit", fail_commit)

        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("private-commit-detail", response.text)
        with self.engine.connect() as connection:
            stored_id = connection.scalar(
                select(VehicleRecord.id).where(VehicleRecord.vehicle_id == self.payload["vehicle_id"])
            )
        self.assertIsNone(stored_id)

    def test_agent_queries_vehicle_created_through_api(self):
        body = self.payload | {"price_cny": 128600, "mileage_km": 32700}
        created = self.client.post("/api/vehicles", json=body)
        self.assertEqual(created.status_code, 201, created.text)

        # 仅替换模型；录入、图执行、工具查询都使用真实代码和 PostgreSQL。
        with patch.object(graph_module, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="", tool_calls=[{
                    "name": "get_vehicle",
                    "args": {"vehicle_id": f"  {body['vehicle_id']}  "},
                    "id": "inventory-lookup", "type": "tool_call",
                }]),
                AIMessage(content="该车售价 128600 元，里程 32700 公里。"),
            ]
            response = self.client.post(
                "/api/chat", json={"message": f"{body['vehicle_id']} 多少钱，多少公里？"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        expected = body | {"is_demo": False}
        self.assertEqual(response.json()["vehicle"], expected)
        result = next(
            message for message in model.invoke.call_args_list[1].args[0]
            if isinstance(message, ToolMessage)
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.tool_call_id, "inventory-lookup")
        self.assertEqual(json.loads(result.content), expected)

    def test_agent_handles_vehicle_absent_from_database(self):
        code = self.new_code()
        with patch.object(graph_module, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="", tool_calls=[{
                    "name": "get_vehicle", "args": {"vehicle_id": code},
                    "id": "missing-inventory", "type": "tool_call",
                }]),
                AIMessage(content="未找到该编号对应的车辆，请核对编号。"),
            ]
            response = self.client.post("/api/chat", json={"message": f"查询 {code}"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["vehicle"])
        result = model.invoke.call_args_list[1].args[0][-1]
        self.assertEqual(result.status, "error")
        self.assertIn("未找到", result.content)

    def test_sql_like_vehicle_id_cannot_match_other_inventory(self):
        created = self.client.post("/api/vehicles", json=self.payload)
        self.assertEqual(created.status_code, 201, created.text)
        with self.assertRaisesRegex(vehicle_tools.VehicleToolError, "未找到"):
            vehicle_tools.get_vehicle.invoke({"vehicle_id": "' OR 1=1 --"})
        stored = vehicle_tools.get_vehicle.invoke({"vehicle_id": self.payload["vehicle_id"]})
        self.assertEqual(stored["vehicle_id"], self.payload["vehicle_id"])


if __name__ == "__main__":
    unittest.main()
