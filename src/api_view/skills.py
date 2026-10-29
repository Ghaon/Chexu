from fastapi import APIRouter, Depends

from src.agent.skills import get_skill_registry, SkillMetadata
from src.api_view.auth import get_current_employee
from src.api_view.schemas import EmployeeResponse


router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("", response_model=list[SkillMetadata])
def list_skills(employee: EmployeeResponse = Depends(get_current_employee)) -> list[SkillMetadata]:
    return get_skill_registry().list_metadata(employee.role)
