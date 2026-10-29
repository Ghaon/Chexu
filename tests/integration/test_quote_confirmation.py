from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.agent.config import Settings
from src.api_view.app import app
from src.api_view import quotes as quotes_api
from src.api_view.auth import get_current_employee, require_quote_confirmer
from src.api_view.schemas import EmployeeResponse
from src.database.models import EmployeeRecord, QuoteRecord, VehicleRecord


class QuoteConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(Settings().database_url.get_secret_value(),
                                   connect_args={"connect_timeout": 5}, hide_parameters=True)
        cls.factory = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        code = f"CONFIRM-{uuid4().hex[:20]}"
        with self.factory.begin() as session:
            employee = EmployeeRecord(username=f"confirm_{uuid4().hex[:16]}", role="manager",
                                      password_hash="unused-by-confirmation-state-tests")
            session.add(employee)
            record = VehicleRecord(vehicle_id=code, title="报价确认检查车辆",
                                   price_cny=86000, mileage_km=52000, is_demo=True)
            session.add(record)
            session.flush()
            self.vehicle_record_id = record.id
            self.employee = EmployeeResponse.model_validate(employee)
        self.addCleanup(self.remove_test_data)
        factory_patch = patch.object(quotes_api, "SessionFactory", self.factory)
        factory_patch.start()
        self.addCleanup(factory_patch.stop)
        permission_patch = patch.dict(app.dependency_overrides, {
            get_current_employee: lambda: self.employee,
            require_quote_confirmer: lambda: self.employee,
        })
        permission_patch.start()
        self.addCleanup(permission_patch.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        created = self.client.post("/api/quotes", json={
            "vehicle_id": code, "expected_price_cny": 86000, "discount_cny": 2000,
        })
        self.assertEqual(created.status_code, 201, created.text)
        self.draft = created.json()
        self.quote_id = self.draft["quote_id"]
        self.path = f"/api/quotes/{self.quote_id}/confirm"

    def remove_test_data(self):
        # 先删除本次车辆关联的报价，再删除车辆；保留其他业务记录。
        with self.engine.begin() as connection:
            connection.execute(delete(QuoteRecord).where(QuoteRecord.vehicle_record_id == self.vehicle_record_id))
            connection.execute(delete(VehicleRecord).where(VehicleRecord.id == self.vehicle_record_id))
            connection.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id == self.employee.employee_id))

    def test_confirm_preserves_amounts_and_repeated_confirmation_is_idempotent(self):
        confirmed = self.client.post(self.path, json={"confirm": True})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        body = confirmed.json()
        self.assertEqual(body["status"], "confirmed")
        self.assertFalse(body["requires_confirmation"])
        self.assertFalse(body["is_total_price"])
        self.assertIsNotNone(datetime.fromisoformat(body["confirmed_at"]).tzinfo)
        for key in ("quote_id", "listed_price_cny", "discount_cny", "quoted_price_cny",
                    "pending_items", "generated_at", "created_at"):
            self.assertEqual(body[key], self.draft[key])
        fetched = self.client.get(f"/api/quotes/{self.quote_id}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json(), body)
        with self.factory.begin() as session:
            session.get(VehicleRecord, self.vehicle_record_id).price_cny = 90000
        repeated = self.client.post(self.path, json={"confirm": True})
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), body)

    def test_invalid_confirmation_and_changed_inventory_leave_draft_unchanged(self):
        self.assertEqual(self.client.post(self.path).status_code, 422)
        for payload in ({}, {"confirm": False}, {"confirm": True, "quoted_price_cny": 1}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(self.path, json=payload).status_code, 422)
        self.assertEqual(self.client.post(f"/api/quotes/{uuid4()}/confirm", json={"confirm": True}).status_code, 404)
        with self.factory.begin() as session:
            session.get(VehicleRecord, self.vehicle_record_id).price_cny = 90000
        response = self.client.post(self.path, json={"confirm": True})
        self.assertEqual(response.status_code, 409, response.text)
        fetched = self.client.get(f"/api/quotes/{self.quote_id}")
        self.assertEqual(fetched.json(), self.draft)
        self.assertIsNone(fetched.json()["confirmed_at"])

    def test_concurrent_confirmation_returns_the_same_confirmation_time(self):
        ready = Barrier(2)

        def confirm():
            with TestClient(app) as client:
                ready.wait(timeout=10)
                response = client.post(self.path, json={"confirm": True})
                return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(confirm) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual([result[0] for result in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(results[0][1]["status"], "confirmed")

    def test_commit_failure_rolls_back_confirmation_and_allows_retry(self):
        def fail_commit(connection):
            raise OperationalError(None, None, RuntimeError("private-confirm-detail"))

        event.listen(self.engine, "commit", fail_commit)
        try:
            with self.assertLogs(quotes_api.logger, "WARNING"):
                response = self.client.post(self.path, json={"confirm": True})
        finally:
            event.remove(self.engine, "commit", fail_commit)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("private-confirm-detail", response.text)
        with self.factory() as session:
            stored = session.get(QuoteRecord, UUID(self.quote_id))
            self.assertEqual(stored.status, "draft")
            self.assertTrue(stored.requires_confirmation)
            self.assertIsNone(stored.confirmed_at)
        retried = self.client.post(self.path, json={"confirm": True})
        self.assertEqual(retried.status_code, 200, retried.text)


if __name__ == "__main__":
    unittest.main()
