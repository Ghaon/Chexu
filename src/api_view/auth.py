from datetime import datetime, timedelta, timezone
import logging
import math
import re

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError

from src.agent.config import get_settings
from src.api_view.schemas import EmployeeLogin, EmployeeResponse, LoginResponse, PasswordChange
from src.database.connection import SessionFactory
from src.database.models import EmployeeRecord, EmployeeTokenRecord, LoginAttemptRecord
from src.security import DUMMY_PASSWORD_HASH, create_access_token, hash_password, token_digest, verify_password


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


def unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="登录信息无效或已过期，请重新登录。",
                          headers={"WWW-Authenticate": "Bearer"})


def storage_error(error: SQLAlchemyError) -> HTTPException:
    logger.warning("Authentication storage failed (%s).", type(error).__name__)
    if isinstance(error, (OperationalError, TimeoutError)):
        return HTTPException(status_code=503, detail="登录服务暂时不可用，请稍后重试。")
    return HTTPException(status_code=500, detail="登录服务处理失败，请检查服务端日志。")


@router.post("/login", response_model=LoginResponse)
def login(body: EmployeeLogin) -> LoginResponse:
    settings = get_settings()
    username = body.username.lower()
    failure = None
    response = None
    try:
        with SessionFactory.begin() as session:
            now = datetime.now(timezone.utc)
            session.execute(insert(LoginAttemptRecord).values(username=username, updated_at=now)
                            .on_conflict_do_update(index_elements=["username"], set_={"username": username}))
            attempt = session.get(LoginAttemptRecord, username, with_for_update=True)
            if attempt.locked_until is not None and attempt.locked_until > now:
                retry_after = max(1, math.ceil((attempt.locked_until - now).total_seconds()))
                failure = HTTPException(status_code=429, detail="登录失败次数过多，请稍后重试。",
                                        headers={"Retry-After": str(retry_after)})
            else:
                if attempt.locked_until is not None:
                    attempt.failed_attempts = 0
                    attempt.locked_until = None
                employee = session.scalar(select(EmployeeRecord).where(
                    EmployeeRecord.username == username
                ).with_for_update())
                valid = verify_password(body.password.get_secret_value(),
                                         employee.password_hash if employee else DUMMY_PASSWORD_HASH)
                attempt.updated_at = now
                if employee is None or not valid or not employee.is_active:
                    attempt.failed_attempts += 1
                    failure = unauthorized()
                    if attempt.failed_attempts >= settings.login_max_failures:
                        attempt.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
                        failure = HTTPException(status_code=429, detail="登录失败次数过多，请稍后重试。",
                                                headers={"Retry-After": str(settings.login_lock_minutes * 60)})
                else:
                    attempt.failed_attempts = 0
                    token = create_access_token()
                    expires_at = now + timedelta(minutes=settings.access_token_ttl_minutes)
                    session.add(EmployeeTokenRecord(token_hash=token_digest(token),
                                                     employee_id=employee.employee_id, expires_at=expires_at))
                    response = LoginResponse(access_token=token, expires_at=expires_at,
                                             employee=EmployeeResponse.model_validate(employee))
        # 先提交失败计数，再返回认证错误，避免异常导致计数随事务回滚。
        if failure is not None:
            raise failure
        assert response is not None
        return response
    except SQLAlchemyError as exc:
        raise storage_error(exc) from exc


def get_current_employee(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> EmployeeResponse:
    if credentials is None or re.fullmatch(r"[A-Za-z0-9_-]{43}", credentials.credentials) is None:
        raise unauthorized()
    try:
        with SessionFactory() as session:
            employee = session.scalar(
                select(EmployeeRecord).join(EmployeeTokenRecord).where(
                    EmployeeTokenRecord.token_hash == token_digest(credentials.credentials),
                    EmployeeTokenRecord.expires_at > func.now(),
                    EmployeeRecord.is_active.is_(True),
                )
            )
            if employee is None:
                raise unauthorized()
            return EmployeeResponse.model_validate(employee)
    except SQLAlchemyError as exc:
        raise storage_error(exc) from exc


def require_quote_confirmer(employee: EmployeeResponse = Depends(get_current_employee)) -> EmployeeResponse:
    if employee.role != "manager":
        raise HTTPException(status_code=403, detail="只有经理可以确认报价。")
    return employee


@router.get("/me", response_model=EmployeeResponse)
def current_employee(employee: EmployeeResponse = Depends(get_current_employee)) -> EmployeeResponse:
    return employee


@router.post("/logout", status_code=204)
def logout(employee: EmployeeResponse = Depends(get_current_employee),
           credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Response:
    if credentials is None:
        raise unauthorized()
    try:
        with SessionFactory.begin() as session:
            session.execute(delete(EmployeeTokenRecord).where(
                EmployeeTokenRecord.employee_id == employee.employee_id,
                EmployeeTokenRecord.token_hash == token_digest(credentials.credentials),
            ))
        return Response(status_code=204)
    except SQLAlchemyError as exc:
        raise storage_error(exc) from exc


@router.post("/change-password", status_code=204)
def change_password(body: PasswordChange,
                    employee: EmployeeResponse = Depends(get_current_employee)) -> Response:
    try:
        with SessionFactory.begin() as session:
            # 与登录保持相同锁顺序：先失败计数，再员工记录。
            attempt = session.get(LoginAttemptRecord, employee.username, with_for_update=True)
            actor = session.get(EmployeeRecord, employee.employee_id, with_for_update=True)
            if actor is None or not actor.is_active:
                raise unauthorized()
            if not verify_password(body.current_password.get_secret_value(), actor.password_hash):
                raise HTTPException(status_code=400, detail="当前密码不正确。")
            actor.password_hash = hash_password(body.new_password.get_secret_value())
            session.execute(delete(EmployeeTokenRecord).where(EmployeeTokenRecord.employee_id == actor.employee_id))
            if attempt is not None:
                attempt.failed_attempts = 0
                attempt.locked_until = None
                attempt.updated_at = datetime.now(timezone.utc)
        return Response(status_code=204)
    except SQLAlchemyError as exc:
        raise storage_error(exc) from exc
