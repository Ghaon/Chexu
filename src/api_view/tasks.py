import asyncio
from functools import partial
import json
from time import monotonic
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError

from src.agent.config import get_settings
from src.api_view.auth import get_current_employee
from src.api_view.runs import run_error
from src.api_view.schemas import ChatRequest, ChatResponse, EmployeeResponse, TaskResultResponse
from src.domain.tasks import ACTIVE_TASK_STATUSES, TaskConflict, TaskEventPage, TaskNotFound, TaskView
from src.services import tasks


router = APIRouter(prefix="/api/tasks", tags=["agent-tasks"])


def task_error(error: Exception):
    if isinstance(error, TaskNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (TaskConflict, IntegrityError)):
        return HTTPException(status_code=409, detail=str(error) if isinstance(error, TaskConflict) else "该会话已有活动任务。")
    return run_error(error)


@router.post("", response_model=TaskView, status_code=202)
def submit_task(body: ChatRequest, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return tasks.submit(body.message, body.thread_id, body.skill_id, employee.employee_id, employee.role)
    except Exception as exc:
        raise task_error(exc) from exc


@router.get("", response_model=list[TaskView])
def list_tasks(thread_id: UUID | None = None, limit: int = Query(default=20, ge=1, le=100),
                employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return tasks.list_tasks(employee.employee_id, thread_id, limit)
    except Exception as exc:
        raise task_error(exc) from exc


@router.get("/{task_id}", response_model=TaskView)
def get_task(task_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return tasks.get_task(task_id, employee.employee_id)
    except Exception as exc:
        raise task_error(exc) from exc


@router.get("/{task_id}/result", response_model=TaskResultResponse)
def get_result(task_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        task, result = tasks.get_result(task_id, employee.employee_id)
        return TaskResultResponse(task=task, result=ChatResponse.model_validate(result) if result is not None else None)
    except Exception as exc:
        raise task_error(exc) from exc


@router.post("/{task_id}/cancel", response_model=TaskView, status_code=202)
def cancel_task(task_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return tasks.request_cancel(task_id, employee.employee_id)
    except Exception as exc:
        raise task_error(exc) from exc


@router.get("/{task_id}/events", response_model=TaskEventPage)
def list_events(task_id: UUID, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200),
                  employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return tasks.list_events(task_id, employee.employee_id, after, limit)
    except Exception as exc:
        raise task_error(exc) from exc


def sse_event(event) -> str:
    payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    return f"id: {event.sequence}\nevent: {event.event}\ndata: {payload}\n\n"


@router.get("/{task_id}/events/stream", response_class=StreamingResponse)
async def stream_events(task_id: UUID, request: Request, after: int = Query(default=0, ge=0),
                         last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                         employee: EmployeeResponse = Depends(get_current_employee)):
    if last_event_id is not None:
        try:
            cursor = int(last_event_id)
            if cursor < 0:
                raise ValueError()
            after = max(after, cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Last-Event-ID 必须是非负事件序号。") from exc
    try:
        await anyio.to_thread.run_sync(tasks.get_task, task_id, employee.employee_id)
    except Exception as exc:
        raise task_error(exc) from exc

    async def generate():
        cursor, started, heartbeat = after, monotonic(), monotonic()
        while monotonic() - started < get_settings().task_stream_seconds:
            if await request.is_disconnected():
                return
            try:
                page = await anyio.to_thread.run_sync(partial(tasks.list_events, task_id, employee.employee_id, cursor))
            except Exception:
                yield 'event: stream.error\ndata: {"message":"事件读取暂时失败，请按最后序号重连。"}\n\n'
                return
            for event in page.events:
                cursor = event.sequence
                yield sse_event(event)
            if page.status not in ACTIVE_TASK_STATUSES and not page.has_more:
                return
            if monotonic() - heartbeat >= 5:
                yield ": heartbeat\n\n"
                heartbeat = monotonic()
            if not page.has_more:
                await asyncio.sleep(get_settings().task_poll_seconds)
        yield ": reconnect-with-last-event-id\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
