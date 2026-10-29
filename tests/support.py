from unittest.mock import patch
from uuid import uuid4


def install_test_identity(testcase, factory=None):
    """为非鉴权用例提供身份，权限与归属边界由独立集成用例验证。"""
    from sqlalchemy import delete
    from src.api_view.app import app
    from src.api_view.auth import get_current_employee
    from src.api_view.schemas import EmployeeResponse
    from src.database.models import EmployeeRecord

    if factory is None:
        employee = EmployeeResponse(employee_id=uuid4(), username="test_manager", role="manager")
    else:
        with factory.begin() as session:
            record = EmployeeRecord(username=f"test_{uuid4().hex[:16]}", role="manager",
                                    password_hash="unused-by-non-auth-tests")
            session.add(record)
            session.flush()
            employee = EmployeeResponse.model_validate(record)

        def remove_employee():
            with factory.begin() as session:
                session.execute(delete(EmployeeRecord).where(EmployeeRecord.employee_id == employee.employee_id))

        testcase.addCleanup(remove_employee)

    override = patch.dict(app.dependency_overrides, {get_current_employee: lambda: employee})
    override.start()
    testcase.addCleanup(override.stop)
    ownership = patch("src.agent.runtime.ensure_conversation")
    ownership.start()
    testcase.addCleanup(ownership.stop)
    pending = patch("src.services.tasks.ensure_thread_available")
    pending.start()
    testcase.addCleanup(pending.stop)
    memory_context = patch("src.services.memories.confirmed_context", return_value="")
    memory_context.start()
    testcase.addCleanup(memory_context.stop)
    for name in ("pending", "reject_pending"):
        patcher = patch(f"src.services.plan_actions.{name}", return_value=None)
        patcher.start()
        testcase.addCleanup(patcher.stop)
    # 这些用例隔离运行记录存储；真实控制、权限和持久化由运行控制集成用例覆盖。
    for name, result in {"start_run": None, "finish_run": None, "charge": None,
                         "cancellation_requested": False, "latest": None, "usage": (0, 0, 0)}.items():
        patcher = patch(f"src.services.executions.{name}", return_value=result)
        patcher.start()
        testcase.addCleanup(patcher.stop)
    return employee
