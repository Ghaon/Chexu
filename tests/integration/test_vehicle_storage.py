import unittest
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.agent.config import Settings
from src.database.models import VehicleRecord


class VehicleStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            Settings().database_url.get_secret_value(),
            connect_args={"connect_timeout": 5},
            hide_parameters=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        # 每项检查结束后回滚外层事务，不保留检查车辆。
        self.session = Session(bind=self.connection, join_transaction_mode="create_savepoint")

    def tearDown(self):
        self.session.close()
        self.transaction.rollback()
        self.connection.close()

    def new_vehicle(self, **changes):
        values = {
            "vehicle_id": f"CHECK-{uuid4().hex[:20]}",
            "title": "数据库约束检查车辆",
            "price_cny": 96800,
            "mileage_km": 64000,
        }
        values.update(changes)
        return VehicleRecord(**values)

    def assert_rejected(self, vehicle, constraint_name):
        self.session.add(vehicle)
        with self.assertRaises(IntegrityError) as error:
            self.session.flush()
        self.assertEqual(error.exception.orig.diag.constraint_name, constraint_name)

    def test_record_can_be_written_and_queried_with_database_defaults(self):
        vehicle = self.new_vehicle()
        self.session.add(vehicle)
        self.session.flush()
        self.assertIsInstance(vehicle.id, int)
        self.assertFalse(vehicle.is_demo)
        self.assertIsNotNone(vehicle.created_at.tzinfo)
        found = self.session.scalars(
            select(VehicleRecord).where(VehicleRecord.vehicle_id == vehicle.vehicle_id)
        ).one()
        self.assertEqual(found.id, vehicle.id)

    def test_duplicate_vehicle_id_is_rejected(self):
        vehicle = self.new_vehicle()
        self.session.add(vehicle)
        self.session.flush()
        self.assert_rejected(
            self.new_vehicle(vehicle_id=vehicle.vehicle_id), "uq_vehicles_vehicle_id"
        )

    def test_negative_price_is_rejected(self):
        self.assert_rejected(self.new_vehicle(price_cny=-1), "ck_vehicles_price_nonnegative")

    def test_negative_mileage_is_rejected(self):
        self.assert_rejected(self.new_vehicle(mileage_km=-1), "ck_vehicles_mileage_nonnegative")

    def test_blank_vehicle_id_is_rejected(self):
        self.assert_rejected(self.new_vehicle(vehicle_id="   "), "ck_vehicles_id_not_blank")

    def test_blank_title_is_rejected(self):
        self.assert_rejected(self.new_vehicle(title="   "), "ck_vehicles_title_not_blank")


if __name__ == "__main__":
    unittest.main()
