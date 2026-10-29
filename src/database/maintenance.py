from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_

from src.database.connection import SessionFactory, engine
from src.database.models import EmployeeTokenRecord, LoginAttemptRecord


def prune_auth() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    with SessionFactory.begin() as session:
        tokens = session.execute(delete(EmployeeTokenRecord).where(EmployeeTokenRecord.expires_at <= now)).rowcount
        attempts = session.execute(delete(LoginAttemptRecord).where(
            LoginAttemptRecord.updated_at < now - timedelta(days=7),
            or_(LoginAttemptRecord.locked_until.is_(None), LoginAttemptRecord.locked_until <= now),
        )).rowcount
    return tokens, attempts


if __name__ == "__main__":
    try:
        tokens, attempts = prune_auth()
        print(f"Removed expired tokens: {tokens}; stale login counters: {attempts}")
    finally:
        engine.dispose()
