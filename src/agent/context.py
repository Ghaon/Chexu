from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from src.agent.budget import ExecutionBudget
from src.agent.config import get_settings
from src.agent.skills import SkillSnapshot
from src.domain.execution import ExecutionCancelled, ExecutionTimedOut
from src.services import executions


@dataclass
class RunControl:
    run_id: UUID
    turn_id: UUID
    deadline: float
    baseline_model: int = 0
    baseline_tools: int = 0
    duration_seconds: float = 60
    step_id: str | None = None
    step_model_limit: int | None = None
    step_tool_limit: int | None = None

    def check(self) -> None:
        if executions.cancellation_requested(self.run_id):
            raise ExecutionCancelled("本次执行已取消。")
        if monotonic() >= self.deadline:
            raise ExecutionTimedOut("本次执行已超时，可在运行记录中恢复未完成的步骤。")

    def timeout(self, ceiling: float) -> float:
        self.check()
        return max(0.001, min(ceiling, self.deadline - monotonic()))

    def charge(self, resource: str, count: int, limit: int) -> None:
        self.check()
        baseline = self.baseline_model if resource == "model_calls" else self.baseline_tools
        if self.step_id is None:
            executions.charge(self.run_id, self.turn_id, resource, count, limit, baseline)
        else:
            step_limit = self.step_model_limit if resource == "model_calls" else self.step_tool_limit
            executions.charge(self.run_id, self.turn_id, resource, count, limit, baseline,
                              step_id=self.step_id, step_limit=step_limit)


@dataclass(frozen=True)
class AgentContext:
    employee_id: UUID
    role: str
    thread_id: UUID
    run_id: UUID
    turn_id: UUID
    skill: SkillSnapshot
    budget: ExecutionBudget
    control: RunControl
    task_id: UUID | None = None
    agent_id: UUID | None = None

    def snapshot(self) -> dict:
        return {"skill": self.skill.model_dump(mode="json"), "role": self.role,
                "baseline_model": self.control.baseline_model, "baseline_tools": self.control.baseline_tools,
                "max_model_calls": self.budget.max_model_calls, "max_tool_calls": self.budget.max_tool_calls,
                "max_run_seconds": self.control.duration_seconds,
                "task_id": str(self.task_id) if self.task_id else None}


def create_context(employee_id: UUID, role: str, thread_id: UUID, run_id: UUID, turn_id: UUID,
                   skill: SkillSnapshot, *, used_model: int = 0, used_tools: int = 0,
                   baseline_model: int = 0, baseline_tools: int = 0, settings=None, task_id: UUID | None = None) -> AgentContext:
    settings = settings or get_settings()
    planned = skill.id == "deal_review"
    model_limit = settings.plan_max_model_calls if planned else settings.max_model_calls
    tool_limit = settings.plan_max_tool_calls if planned else settings.max_tool_calls
    duration = settings.plan_max_run_seconds if planned else settings.max_run_seconds
    return AgentContext(employee_id, role, thread_id, run_id, turn_id, skill,
                        ExecutionBudget(min(model_limit, skill.max_model_calls),
                                        min(tool_limit, skill.max_tool_calls), used_model, used_tools),
                        RunControl(run_id, turn_id, monotonic() + duration,
                                   baseline_model, baseline_tools, duration), task_id)
