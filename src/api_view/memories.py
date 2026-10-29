from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.api_view.auth import get_current_employee
from src.api_view.schemas import EmployeeResponse
from src.domain.memory import MemoryConfirm, MemoryCreate, MemoryUpdate, MemoryView
from src.services import memories


router = APIRouter(prefix="/api/memories", tags=["long-term-memory"])


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except memories.MemoryNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except memories.MemoryConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, "记忆内容与类别不匹配。") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "记忆存储暂时不可用。") from exc


@router.get("", response_model=list[MemoryView])
def list_memories(status: Literal["pending", "confirmed"] | None = None,
                  employee: EmployeeResponse = Depends(get_current_employee)):
    return _call(memories.list_memories, employee.employee_id, status)


@router.get("/{memory_id}", response_model=MemoryView)
def get_memory(memory_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    return _call(memories.get_memory, employee.employee_id, memory_id)


@router.post("", response_model=MemoryView, status_code=201)
def create_memory(body: MemoryCreate, employee: EmployeeResponse = Depends(get_current_employee)):
    return _call(memories.save_confirmed, employee.employee_id, body)


@router.post("/{memory_id}/confirm", response_model=MemoryView)
def confirm_memory(memory_id: UUID, body: MemoryConfirm, employee: EmployeeResponse = Depends(get_current_employee)):
    return _call(memories.confirm_memory, employee.employee_id, memory_id, body.expected_revision)


@router.patch("/{memory_id}", response_model=MemoryView)
def update_memory(memory_id: UUID, body: MemoryUpdate, employee: EmployeeResponse = Depends(get_current_employee)):
    return _call(memories.confirm_memory, employee.employee_id, memory_id, body.expected_revision, value=body.value)


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: UUID, expected_revision: int = Query(ge=1),
                  employee: EmployeeResponse = Depends(get_current_employee)):
    _call(memories.delete_memory, employee.employee_id, memory_id, expected_revision)
    return Response(status_code=204)
