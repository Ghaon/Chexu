import json
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.agent.config import Settings
from src.agent import vehicle_agent as graph_module
from src.agent.tools import vehicle_tools
from src.api_view.app import app
from src.database.models import VehicleRecord
from tests.support import install_test_identity


class VehicleSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            Settings().database_url.get_secret_value(),
            connect_args={"connect_timeout": 5}, hide_parameters=True,
        )

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
        self.connection = self.engine.connect()
        self.addCleanup(self.connection.close)
        self.transaction = self.connection.begin()
        self.addCleanup(self.transaction.rollback)
        # 临时表仅对本连接可见，隔离已有库存；外层事务回滚后自动移除。
        self.connection.execute(text(
            "CREATE TEMP TABLE vehicles "
            "(LIKE public.vehicles INCLUDING ALL) ON COMMIT DROP"
        ))
        self.factory = sessionmaker(
            bind=self.connection, join_transaction_mode="create_savepoint"
        )
        with self.factory.begin() as session:
            for code, price, mileage in (
                ("A", 60000, 60000), ("B", 80000, 50000),
                ("C", 80000, 40000), ("D", 59999, 30000),
                ("E", 100001, 30000), ("F", 90000, 60001),
            ):
                session.add(VehicleRecord(
                    vehicle_id=code, title=f"筛选检查车辆 {code}",
                    price_cny=price, mileage_km=mileage,
                ))
        factory_patch = patch.object(vehicle_tools, "SessionFactory", self.factory)
        factory_patch.start()
        self.addCleanup(factory_patch.stop)

    def test_filtered_inventory_flows_through_chat_and_reports_more_results(self):
        with TestClient(app) as client, patch.object(graph_module, "tool_model") as model:
            model.invoke.side_effect = [
                AIMessage(content="", tool_calls=[{
                    "name": "search_vehicles",
                    "args": {
                        "min_price_cny": 60000, "max_price_cny": 100000,
                        "max_mileage_km": 60000, "limit": 2,
                    },
                    "id": "search-1", "type": "tool_call",
                }]),
                AIMessage(content="先展示两辆匹配车辆，还有更多结果。"),
            ]
            response = client.post("/api/chat", json={
                "message": "找6到10万元、6万公里以内的车，先看2辆。"
            })
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["vehicle"])
        self.assertEqual([v["vehicle_id"] for v in body["vehicles"]], ["A", "B"])
        self.assertTrue(body["has_more"])
        tool_result = model.invoke.call_args_list[1].args[0][-1]
        self.assertEqual(tool_result.status, "success")
        self.assertEqual(json.loads(tool_result.content)["vehicles"], body["vehicles"])

        full = vehicle_tools.search_vehicles.invoke({
            "min_price_cny": 60000, "max_price_cny": 100000, "max_mileage_km": 60000,
        })
        self.assertEqual([v["vehicle_id"] for v in full["vehicles"]], ["A", "B", "C"])
        self.assertFalse(full["has_more"])
        self.assertEqual(vehicle_tools.search_vehicles.invoke({"max_price_cny": 0}), {
            "vehicles": [], "has_more": False,
        })

    def test_invalid_filters_do_not_query_database(self):
        with patch.object(vehicle_tools, "SessionFactory") as factory:
            for args in (
                {"max_price_cny": -1}, {"limit": 0}, {"limit": 21},
                {"max_mileage_km": True}, {"max_price_cny": 1.5},
            ):
                with self.subTest(args=args), self.assertRaises(ValidationError):
                    vehicle_tools.search_vehicles.invoke(args)
            with self.assertRaisesRegex(vehicle_tools.VehicleToolError, "最低价格"):
                vehicle_tools.search_vehicles.invoke({"min_price_cny": 10, "max_price_cny": 5})
            factory.assert_not_called()

    def test_failed_search_does_not_return_previous_vehicle(self):
        result = graph_module.finish({"messages": [
            ToolMessage(content=json.dumps({"vehicle_id": "A"}),
                        name="get_vehicle", tool_call_id="old", status="success"),
            ToolMessage(content="车辆数据库暂时不可用。", name="search_vehicles",
                        tool_call_id="new", status="error"),
            AIMessage(content="本次筛选失败，请稍后重试。"),
        ]})
        self.assertIsNone(result["vehicle"])
        self.assertEqual(result["vehicles"], [])
        self.assertFalse(result["has_more"])


if __name__ == "__main__":
    unittest.main()
