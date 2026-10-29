import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from src.agent.config import Settings
from src.api_view.app import app
from src.api_view import quotes as quotes_api
from src.database.models import QuoteRecord, VehicleRecord
from src.domain.quotes import build_quote_draft
from src.version import STAGE, VERSION
from tests.support import install_test_identity


class QuoteStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(Settings().database_url.get_secret_value(),
                                   connect_args={"connect_timeout": 5}, hide_parameters=True)
        cls.factory = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        install_test_identity(self, self.factory)
        self.code = f"SAVE-{uuid4().hex[:20]}"
        with self.factory.begin() as session:
            vehicle = VehicleRecord(vehicle_id=self.code, title="报价保存检查车辆",
                                    price_cny=86000, mileage_km=52000, is_demo=True)
            session.add(vehicle)
            session.flush()
            self.vehicle_record_id = vehicle.id
        self.addCleanup(self.remove_test_data)
        factory_patch = patch.object(quotes_api, "SessionFactory", self.factory)
        factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.payload = {"vehicle_id": self.code, "expected_price_cny": 86000, "discount_cny": 2000}

    def remove_test_data(self):
        # 仅删除本次创建车辆对应的报价，再删除该测试车辆。
        with self.engine.begin() as connection:
            connection.execute(delete(QuoteRecord).where(QuoteRecord.vehicle_record_id == self.vehicle_record_id))
            connection.execute(delete(VehicleRecord).where(VehicleRecord.id == self.vehicle_record_id))

    def count_quotes(self):
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(QuoteRecord).where(
                QuoteRecord.vehicle_record_id == self.vehicle_record_id
            ))

    def test_save_commits_snapshot_and_lookup_preserves_it_after_inventory_changes(self):
        response = self.client.post("/api/quotes", json=self.payload)
        self.assertEqual(response.status_code, 201, response.text)
        saved = response.json()
        quote_id = UUID(saved["quote_id"])
        self.assertEqual(saved["quoted_price_cny"], 84000)
        self.assertEqual(saved["status"], "draft")
        self.assertTrue(saved["requires_confirmation"])
        self.assertFalse(saved["is_total_price"])
        with self.factory() as session:
            self.assertIsNotNone(session.get(QuoteRecord, quote_id))
        with self.factory.begin() as session:
            vehicle = session.get(VehicleRecord, self.vehicle_record_id)
            vehicle.price_cny = 90000
            vehicle.title = "已修改的库存名称"
        fetched = self.client.get(f"/api/quotes/{quote_id}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json(), saved)
        self.assertEqual(self.client.get("/health").json()["stage"], STAGE)
        self.assertEqual(self.client.get("/health").json()["version"], VERSION)

    def test_invalid_or_stale_requests_do_not_create_quotes(self):
        cases = (
            (self.payload | {"expected_price_cny": 85000}, 409),
            (self.payload | {"discount_cny": 86001}, 422),
            (self.payload | {"discount_cny": -1}, 422),
            (self.payload | {"quoted_price_cny": 1}, 422),
            (self.payload | {"status": "confirmed"}, 422),
            (self.payload | {"vehicle_id": f"MISSING-{uuid4().hex[:16]}"}, 404),
            ({"vehicle_id": self.code}, 422),
        )
        for body, status in cases:
            with self.subTest(body=body):
                response = self.client.post("/api/quotes", json=body)
                self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(self.count_quotes(), 0)
        self.assertEqual(self.client.get(f"/api/quotes/{uuid4()}").status_code, 404)
        self.assertEqual(self.client.get("/api/quotes/not-a-uuid").status_code, 422)

    def test_commit_failure_does_not_report_success_or_persist_quote(self):
        def fail_commit(connection):
            raise OperationalError(None, None, RuntimeError("private-commit-detail"))

        event.listen(self.engine, "commit", fail_commit)
        try:
            with self.assertLogs(quotes_api.logger, "WARNING"):
                response = self.client.post("/api/quotes", json=self.payload)
        finally:
            event.remove(self.engine, "commit", fail_commit)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("private-commit-detail", response.text)
        self.assertEqual(self.count_quotes(), 0)

    def test_database_rejects_inconsistent_amounts_and_unconfirmed_status_changes(self):
        draft = build_quote_draft(vehicle_id=self.code, vehicle_title="报价保存检查车辆",
                                  listed_price_cny=86000, discount_cny=2000, is_demo=True)
        for change, constraint in (
            ({"quoted_price_cny": 1}, "ck_quotes_price_calculation"),
            ({"status": "confirmed"}, "ck_quotes_confirmation_state"),
        ):
            with self.subTest(change=change):
                with self.assertRaises(IntegrityError) as error:
                    with self.factory.begin() as session:
                        session.add(QuoteRecord(vehicle_record_id=self.vehicle_record_id,
                                                **(draft.model_dump() | change)))
                        session.flush()
                self.assertEqual(error.exception.orig.diag.constraint_name, constraint)
        self.assertEqual(self.count_quotes(), 0)


if __name__ == "__main__":
    unittest.main()
