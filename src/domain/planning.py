from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


ExecutionSkill = Literal["inventory_match", "vehicle_report", "vehicle_compare", "quote_assist", "web_research"]


class StepInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_ids: list[str] = Field(default_factory=list, max_length=6)
    report_ids: list[UUID] = Field(default_factory=list, max_length=6)
    min_price_cny: int | None = Field(default=None, ge=0)
    max_price_cny: int | None = Field(default=None, ge=0)
    max_mileage_km: int | None = Field(default=None, ge=0)
    discount_cny: int | None = Field(default=None, ge=0)
    notes: str = Field(default="", max_length=1000)


class PlanStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: ExecutionSkill
    goal: str = Field(min_length=1, max_length=300)
    depends_on: list[int] | None = Field(default=None, max_length=6, description="依赖的新步骤编号，从1开始；省略则顺序执行，空列表表示独立。")
    inputs: StepInputs = Field(default_factory=StepInputs)


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStepInput] = Field(min_length=1, max_length=6)


class PlanStepView(BaseModel):
    id: str
    skill_id: ExecutionSkill
    goal: str
    status: Literal["pending", "running", "completed", "needs_input", "blocked", "cancelled", "skipped"]
    result: dict | None = None
    agent_id: UUID | None = None
    depends_on: list[str] = Field(default_factory=list)


class PlanView(BaseModel):
    status: Literal["running", "completed", "needs_input", "awaiting_approval", "cancelled", "failed", "timed_out"]
    current_step: int | None
    steps: list[PlanStepView]
    revision: int = 1
    active_steps: list[str] = Field(default_factory=list)
    replan_count: int = 0
    turn_id: UUID | None = None
    adjustments: list[str] = Field(default_factory=list)


def public_plan(plan: dict | None, *, status: str | None = None, approval: dict | None = None) -> dict | None:
    if plan is None:
        return None
    value = {"status": status or plan["status"], "current_step": plan.get("current_step"),
             "revision": plan.get("revision", 1), "active_steps": plan.get("active_steps", []),
             "replan_count": plan.get("replan_count", 0),
             "turn_id": plan.get("turn_id"),
             "adjustments": plan.get("adjustments", []),
             "steps": [{**{key: step.get(key) for key in ("id", "skill_id", "goal", "status", "result", "agent_id")},
                        "depends_on": step.get("depends_on", [])}
                       for step in plan["steps"]]}
    if approval:
        for step in value["steps"]:
            result = step.get("result") or {}
            if (result.get("approval") or {}).get("approval_id") == approval["approval_id"]:
                step["result"] = {**result, "approval": approval}
    return PlanView.model_validate(value).model_dump(mode="json")


def cancel_plan(plan: dict | None) -> dict | None:
    if plan is None:
        return None
    return {**plan, "status": "cancelled", "steps": [
        {**step, "status": "cancelled" if step["status"] == "running" else "skipped"}
        if step["status"] in {"running", "pending"} else step for step in plan["steps"]], "active_steps": []}


class PlanActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID = Field(default_factory=uuid4)
    expected_revision: int = Field(ge=1)
    expected_turn_id: UUID
    action: Literal["continue", "revise", "replan"]
    message: str = Field(min_length=1, max_length=2000)
    restart_from: str | None = Field(default=None, max_length=64)


class PlanActionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    command_id: UUID
    thread_id: UUID
    task_id: UUID | None
    action: Literal["continue", "revise", "replan"]
    expected_revision: int
    turn_id: UUID | None
    status: Literal["queued", "applied", "rejected", "failed"]
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None
