import logging
import tempfile
import anyio
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from langgraph.errors import GraphRecursionError
from openai import APIConnectionError, APIError, APITimeoutError
from psycopg import Error as PostgresError
from sqlalchemy.exc import SQLAlchemyError

from src.agent.config import PROJECT_ROOT, get_settings
from src.agent.budget import ExecutionBudgetExceeded
from src.agent.runtime import ApprovalPendingError, ConversationInterruptedError, run_chat
from src.agent.tracing import configure_trace_logging
from src.agent.task_worker import TaskWorker
from src.agent.skills import get_skill_registry, SkillError
from src.domain.execution import ExecutionTimedOut
from src.domain.conversation_context import ContextCapacityExceeded
from src.database.checkpoints import ConversationBusyError
from src.database.connection import engine
from src.api_view.schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    EmployeeResponse,
)
from src.api_view.vehicles import router as vehicles_router
from src.api_view.quotes import router as quotes_router
from src.api_view.auth import router as auth_router
from src.api_view.auth import get_current_employee
from src.api_view.conversations import router as conversations_router
from src.api_view.approvals import router as approvals_router
from src.api_view.runs import router as runs_router
from src.api_view.skills import router as skills_router
from src.api_view.reports import router as reports_router, ReportUploadLimitMiddleware
from src.api_view.tasks import router as tasks_router
from src.api_view.plans import router as plans_router
from src.api_view.memories import router as memories_router
from src.reports.storage import prepare_storage
from src.services.conversations import ConversationAccessError
from src.services.tasks import error_details
from src.version import STAGE, VERSION

logger = logging.getLogger(__name__)
settings = get_settings()
configure_trace_logging()
get_skill_registry()
# multipart 大文件的临时内容也放在项目数据目录，避免使用系统盘。
tempfile.tempdir = str(prepare_storage(settings.report_storage_dir) / ".tmp")


@asynccontextmanager
async def lifespan(application: FastAPI):
    worker = TaskWorker() if settings.task_worker_enabled else None
    if worker:
        worker.start()
    try:
        yield
    finally:
        if worker:
            await anyio.to_thread.run_sync(worker.stop)
        engine.dispose()

app = FastAPI(
    title="Chexu API",
    version=VERSION,
    description="Chexu API for Chexu project",
    lifespan=lifespan,
)
app.include_router(vehicles_router)
app.include_router(quotes_router)
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(approvals_router)
app.include_router(runs_router)
app.include_router(skills_router)
app.include_router(reports_router)
app.include_router(tasks_router)
app.include_router(plans_router)
app.include_router(memories_router)
app.add_middleware(ReportUploadLimitMiddleware, max_bytes=settings.max_report_bytes)


@app.get("/health", response_model=HealthResponse)
def health():
    return {
        "status": "ok",
        "stage": STAGE,
        "version": VERSION,
        "mode": settings.mode,
    }


@app.post("/api/chat", response_model=ChatResponse)
def chat(body: ChatRequest, employee: EmployeeResponse = Depends(get_current_employee)):
    thread_id = str(body.thread_id or uuid4())
    run_id = str(uuid4())
    try:
        result = run_chat(body.message, thread_id, run_id, employee.employee_id,
                          role=employee.role, skill_id=body.skill_id)
    except SkillError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc), "thread_id": thread_id, "run_id": run_id}) from exc
    except ContextCapacityExceeded as exc:
        raise HTTPException(status_code=422, detail={"code": "context_capacity_exceeded", "message": str(exc),
                                                   "thread_id": thread_id, "run_id": run_id}) from exc
    except ExecutionTimedOut as exc:
        raise HTTPException(status_code=504, detail={"code": "execution_timed_out", "message": str(exc),
                                                  "thread_id": thread_id, "run_id": run_id}) from exc
    except ApprovalPendingError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "approval_pending", "message": str(exc), "approval_id": str(exc.approval_id),
            "thread_id": thread_id, "run_id": run_id,
        }) from exc
    except ConversationAccessError as exc:
        raise HTTPException(status_code=404, detail={
            "message": str(exc), "thread_id": thread_id, "run_id": run_id,
        }) from exc
    except ExecutionBudgetExceeded as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "execution_budget_exceeded",
                "message": str(exc),
                "resource": exc.resource,
                "limit": exc.limit,
                "used": exc.used,
                "requested": exc.requested,
                "thread_id": thread_id,
                "run_id": run_id,
            },
        ) from exc
    except (ConversationBusyError, ConversationInterruptedError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "thread_id": thread_id, "run_id": run_id},
        ) from exc
    except (PostgresError, SQLAlchemyError) as exc:
        logger.warning("Conversation storage unavailable (%s).", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"message": "会话存储暂时不可用，请稍后重试。", "thread_id": thread_id, "run_id": run_id},
        ) from exc
    except APIConnectionError as exc:
        _, code, message = error_details(exc)
        logger.warning("Model connection failed (%s).", type(exc).__name__)
        raise HTTPException(
            status_code=504 if isinstance(exc, APITimeoutError) else 502,
            detail={"code": code, "message": message, "thread_id": thread_id, "run_id": run_id},
        ) from exc
    except (APIError, GraphRecursionError) as exc:
        logger.warning("Agent execution failed (%s).", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail={"message": "模型执行暂时失败，请检查服务端日志。", "thread_id": thread_id, "run_id": run_id},
        ) from exc
    except Exception as exc:
        logger.error("Unexpected error while executing the agent (%s).", type(exc).__name__)
        raise HTTPException(
            status_code=500,
            detail={"message": "服务端处理失败，请检查服务端日志。", "thread_id": thread_id, "run_id": run_id},
        ) from exc

    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "reply": result["reply"],
        "vehicle": result["vehicle"],
        "vehicles": result["vehicles"],
        "has_more": result["has_more"],
        "quote_draft": result["quote_draft"],
        "status": result.get("status", "completed"),
        "approval": result.get("approval"),
        "quote_id": result.get("quote_id"),
        "skill_id": result.get("skill_id"),
        "skill_version": result.get("skill_version"),
        "report_sources": result.get("report_sources", []),
        "report_analysis": result.get("report_analysis"),
        "plan": result.get("plan"),
        "memory_proposals": result.get("memory_proposals", []),
        "web_sources": result.get("web_sources", []),
        "web_search_status": result.get("web_search_status"),
    }


# 构建后的前端与 API 同源提供，开发时则由 Vite 转发 API 请求。
frontend_dist = PROJECT_ROOT / "frontend" / "dist"
if (frontend_dist / "index.html").is_file():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="workspace")
