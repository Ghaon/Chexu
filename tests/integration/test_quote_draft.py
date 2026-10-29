from contextlib import nullcontext
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from src.agent.config import Settings
from src.agent import vehicle_agent as graph_module
from src.agent import tracing
from src.agent.tools import quote_tools, vehicle_tools
from src.api_view.app import app
from src.database.models import VehicleRecord
from tests.support import install_test_identity


class QuoteDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings()
        cls.engine = create_engine(cls.settings.database_url.get_secret_value(),
                                   connect_args={"connect_timeout": 5}, hide_parameters=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        install_test_identity(self)
        connection = self.engine.connect()
        self.addCleanup(connection.close)
        transaction = connection.begin()
        self.addCleanup(transaction.rollback)
        # 临时表隔离业务库存；检查结束后回滚，不留下测试车辆。
        connection.execute(text("CREATE TEMP TABLE vehicles (LIKE public.vehicles INCLUDING ALL) ON COMMIT DROP"))
        self.factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
        with self.factory.begin() as session:
            session.add_all([
                VehicleRecord(vehicle_id="QUOTE-A", title="报价检查车辆 A", price_cny=68000,
                              mileage_km=40000, is_demo=True),
                VehicleRecord(vehicle_id="QUOTE-B", title="报价检查车辆 B", price_cny=86000,
                              mileage_km=50000, is_demo=True),
            ])
        memory = InMemorySaver()
        for patcher in (
            patch.object(vehicle_tools, "SessionFactory", self.factory),
            patch("src.agent.runtime.open_checkpointer", side_effect=lambda thread_id: nullcontext(memory)),
            patch("src.agent.runtime.get_settings", return_value=self.settings.model_copy(update={
                "max_model_calls": 4, "max_tool_calls": 6,
            })),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def call(self, name, args, call_id):
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": call_id, "type": "tool_call",
        }])

    def test_quote_after_search_uses_current_price_and_returns_structured_draft(self):
        model = FakeMessagesListChatModel(responses=[
            self.call("search_vehicles", {"max_price_cny": 100000}, "search"),
            AIMessage(content="找到两辆模拟车辆。"),
            self.call("create_quote_draft", {"vehicle_id": "QUOTE-B", "discount_cny": 2000}, "quote"),
            AIMessage(content="报价草稿：售价90000元，优惠2000元，车辆报价88000元。其他费用待确认，需人工确认。"),
            AIMessage(content="不客气。"),
        ])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO") as logs:
            first = self.client.post("/api/chat", json={"message": "查十万元以内的车辆"})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["vehicles"][1]["price_cny"], 86000)
            self.assertIsNone(first.json()["quote_draft"])
            with self.factory.begin() as session:
                record = session.scalar(select(VehicleRecord).where(VehicleRecord.vehicle_id == "QUOTE-B"))
                record.price_cny = 90000
            thread_id = first.json()["thread_id"]
            second = self.client.post("/api/chat", json={
                "thread_id": thread_id, "message": "给第二辆生成报价草稿，优惠2000元",
            })
            third = self.client.post("/api/chat", json={"thread_id": thread_id, "message": "谢谢"})
        self.assertEqual(second.status_code, 200, second.text)
        draft = second.json()["quote_draft"]
        self.assertEqual((draft["listed_price_cny"], draft["discount_cny"], draft["quoted_price_cny"]),
                         (90000, 2000, 88000))
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["vehicle_id"], "QUOTE-B")
        self.assertTrue(draft["requires_confirmation"])
        self.assertTrue(draft["is_demo"])
        self.assertFalse(draft["is_total_price"])
        self.assertIn("保险费用", draft["pending_items"])
        self.assertIsNotNone(datetime.fromisoformat(draft["generated_at"]).tzinfo)
        self.assertEqual(third.status_code, 200, third.text)
        self.assertIsNone(third.json()["quote_draft"])
        records = [json.loads(record.getMessage()) for record in logs.records]
        event = next(r for r in records if r["event"] == "tool.start" and r["tool_name"] == "create_quote_draft")
        self.assertEqual(event["arguments"], {"vehicle_id": "QUOTE-B", "discount_cny": 2000})
        with self.factory() as session:
            stored = session.scalar(select(VehicleRecord).where(VehicleRecord.vehicle_id == "QUOTE-B"))
            self.assertEqual(stored.price_cny, 90000)

    def test_invalid_amounts_and_caller_supplied_prices_are_rejected_before_lookup(self):
        with patch.object(quote_tools, "load_vehicle") as lookup:
            for change in (
                {"discount_cny": -1}, {"discount_cny": True}, {"discount_cny": 1.5},
                {"discount_cny": "2000"}, {"discount_cny": 2**31},
                {"listed_price_cny": 1}, {"quoted_price_cny": 0}, {"vehicle_id": "  "},
            ):
                with self.subTest(change=change), self.assertRaises(ValidationError):
                    quote_tools.create_quote_draft.invoke({"vehicle_id": "QUOTE-B"} | change)
            lookup.assert_not_called()

    def test_excessive_discount_hides_earlier_quote_and_missing_vehicle_fails(self):
        model = FakeMessagesListChatModel(responses=[
            self.call("create_quote_draft", {"vehicle_id": "QUOTE-B"}, "first"),
            self.call("create_quote_draft", {"vehicle_id": "QUOTE-B", "discount_cny": 86001}, "invalid"),
            AIMessage(content="优惠超过当前售价，请确认优惠金额。"),
        ])
        with patch.object(graph_module, "tool_model", model), self.assertLogs(tracing.logger, "INFO"):
            response = self.client.post("/api/chat", json={"message": "调整报价草稿"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["quote_draft"])
        with self.assertRaisesRegex(vehicle_tools.VehicleToolError, "未找到"):
            quote_tools.create_quote_draft.invoke({"vehicle_id": "MISSING"})

    def test_default_and_boundary_discounts_preserve_inventory_and_quote_artifact(self):
        draft = quote_tools.create_quote_draft.invoke({"vehicle_id": "QUOTE-B"})
        self.assertEqual(draft["discount_cny"], 0)
        self.assertEqual(draft["quoted_price_cny"], 86000)
        full_discount = quote_tools.create_quote_draft.invoke({"vehicle_id": "QUOTE-B", "discount_cny": 86000})
        self.assertEqual(full_discount["quoted_price_cny"], 0)
        self.assertTrue(full_discount["requires_confirmation"])
        vehicle = vehicle_tools.load_vehicle("QUOTE-B")
        self.assertEqual(vehicle["price_cny"], 86000)
        result = graph_module.finish({"messages": [
            HumanMessage(content="生成报价并查询车辆资料"),
            ToolMessage(content=json.dumps(draft), name="create_quote_draft", tool_call_id="quote"),
            ToolMessage(content=json.dumps(vehicle), name="get_vehicle", tool_call_id="lookup"),
            AIMessage(content="草稿和车辆资料已整理。"),
        ]})
        self.assertEqual(result["quote_draft"], draft)
        self.assertEqual(result["vehicle"], vehicle)


if __name__ == "__main__":
    unittest.main()
