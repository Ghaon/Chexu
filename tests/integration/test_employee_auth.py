from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from src.agent.config import Settings
from src.api_view.app import app
from src.api_view import auth as auth_api
from src.api_view import quotes as quotes_api
from src.database.models import EmployeeRecord, EmployeeTokenRecord, LoginAttemptRecord, QuoteRecord, VehicleRecord
from src.security import hash_password, token_digest


class EmployeeAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(Settings().database_url.get_secret_value(),
                                   connect_args={"connect_timeout": 5}, hide_parameters=True)
        cls.factory = sessionmaker(bind=cls.engine)
        cls.password = "temporary-test-password-2026"
        cls.stored_hash = hash_password(cls.password)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.employees = {}
        self.unknown_username = f"unknown_{uuid4().hex[:16]}"
        code = f"AUTH-{uuid4().hex[:20]}"
        with self.factory.begin() as session:
            for key, role in (("manager", "manager"), ("other_manager", "manager"), ("sales", "sales")):
                employee = EmployeeRecord(username=f"auth_{uuid4().hex[:16]}", role=role,
                                          password_hash=self.stored_hash)
                session.add(employee)
                session.flush()
                self.employees[key] = {"employee_id": employee.employee_id, "username": employee.username}
            vehicle = VehicleRecord(vehicle_id=code, title="登录权限检查车辆", price_cny=86000,
                                    mileage_km=52000, is_demo=True)
            session.add(vehicle)
            session.flush()
            self.vehicle_id = vehicle.id
        self.addCleanup(self.remove_test_data)
        for module in (auth_api, quotes_api):
            patcher = patch.object(module, "SessionFactory", self.factory)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        created = self.client.post("/api/quotes", json={
            "vehicle_id": code, "expected_price_cny": 86000, "discount_cny": 2000,
        }, headers=self.headers(self.login()))
        self.assertEqual(created.status_code, 201, created.text)
        self.quote_id = created.json()["quote_id"]
        self.confirm_path = f"/api/quotes/{self.quote_id}/confirm"

    def remove_test_data(self):
        with self.engine.begin() as connection:
            connection.execute(delete(QuoteRecord).where(QuoteRecord.vehicle_record_id == self.vehicle_id))
            connection.execute(delete(VehicleRecord).where(VehicleRecord.id == self.vehicle_id))
            connection.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id.in_(
                [employee["employee_id"] for employee in self.employees.values()]
            )))
            connection.execute(delete(LoginAttemptRecord).where(LoginAttemptRecord.username.in_(
                [employee["username"] for employee in self.employees.values()] + [self.unknown_username]
            )))

    def login(self, key="manager"):
        response = self.client.post("/api/auth/login", json={
            "username": self.employees[key]["username"], "password": self.password,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_login_checks_password_and_stores_only_password_hash_and_token_digest(self):
        for username, password in ((self.unknown_username, self.password),
                                   (self.employees["manager"]["username"], "wrong-password")):
            response = self.client.post("/api/auth/login", json={"username": username, "password": password})
            self.assertEqual(response.status_code, 401, response.text)
        token = self.login()
        current = self.client.get("/api/auth/me", headers=self.headers(token))
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["employee_id"], str(self.employees["manager"]["employee_id"]))
        self.assertNotIn("password_hash", current.json())
        with self.factory() as session:
            employee = session.get(EmployeeRecord, self.employees["manager"]["employee_id"])
            self.assertTrue(employee.password_hash.startswith("$argon2id$"))
            self.assertNotEqual(employee.password_hash, self.password)
            stored = session.get(EmployeeTokenRecord, token_digest(token))
            self.assertIsNotNone(stored)
            self.assertNotEqual(stored.token_hash, token)
            self.assertGreater(stored.expires_at, datetime.now(timezone.utc))

    def test_confirmation_requires_manager_and_keeps_original_confirmer_on_retry(self):
        unauthorized = self.client.post(self.confirm_path, json={"confirm": True})
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.headers["www-authenticate"], "Bearer")
        sales_token = self.login("sales")
        denied = self.client.post(self.confirm_path, json={"confirm": True}, headers=self.headers(sales_token))
        self.assertEqual(denied.status_code, 403, denied.text)
        manager_token = self.login()
        forged = self.client.post(self.confirm_path, json={
            "confirm": True, "confirmed_by_employee_id": str(self.employees["sales"]["employee_id"]),
        }, headers=self.headers(manager_token))
        self.assertEqual(forged.status_code, 422, forged.text)
        confirmed = self.client.post(self.confirm_path, json={"confirm": True}, headers=self.headers(manager_token))
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["confirmed_by_employee_id"], str(self.employees["manager"]["employee_id"]))
        other_token = self.login("other_manager")
        repeated = self.client.post(self.confirm_path, json={"confirm": True}, headers=self.headers(other_token))
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json(), confirmed.json())

    def test_logout_expiration_and_disabled_employee_invalidate_access(self):
        token = self.login()
        self.assertEqual(self.client.post("/api/auth/logout", headers=self.headers(token)).status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.headers(token)).status_code, 401)
        token = self.login()
        with self.factory.begin() as session:
            session.get(EmployeeTokenRecord, token_digest(token)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(self.client.post(self.confirm_path, json={"confirm": True},
                                         headers=self.headers(token)).status_code, 401)
        token = self.login()
        with self.factory.begin() as session:
            session.get(EmployeeRecord, self.employees["manager"]["employee_id"]).is_active = False
        self.assertEqual(self.client.get("/api/auth/me", headers=self.headers(token)).status_code, 401)
        self.assertEqual(self.client.post(self.confirm_path, json={"confirm": True},
                                         headers=self.headers(token)).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={
            "username": self.employees["manager"]["username"], "password": self.password,
        }).status_code, 401)
        other_token = self.login("other_manager")
        self.assertEqual(self.client.get(f"/api/quotes/{self.quote_id}",
                                         headers=self.headers(other_token)).json()["status"], "draft")

    def test_failed_login_commit_does_not_issue_or_persist_a_token(self):
        def fail_commit(connection):
            raise OperationalError(None, None, RuntimeError("private-login-detail"))

        with self.factory() as session:
            before = session.scalar(select(func.count()).select_from(EmployeeTokenRecord).where(
                EmployeeTokenRecord.employee_id == self.employees["manager"]["employee_id"]
            ))
        event.listen(self.engine, "commit", fail_commit)
        try:
            with self.assertLogs(auth_api.logger, "WARNING") as logs:
                response = self.client.post("/api/auth/login", json={
                    "username": self.employees["manager"]["username"], "password": self.password,
                })
        finally:
            event.remove(self.engine, "commit", fail_commit)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("access_token", response.json())
        self.assertNotIn(self.password, " ".join(logs.output) + response.text)
        self.assertNotIn("private-login-detail", " ".join(logs.output) + response.text)
        with self.factory() as session:
            count = session.scalar(select(func.count()).select_from(EmployeeTokenRecord).where(
                EmployeeTokenRecord.employee_id == self.employees["manager"]["employee_id"]
            ))
            self.assertEqual(count, before)


if __name__ == "__main__":
    unittest.main()
