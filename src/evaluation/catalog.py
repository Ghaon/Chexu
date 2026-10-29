import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals"


class TaskCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=100)
    scenario: Literal["inventory", "compare", "quote", "approval", "report", "memory_pending",
                      "memory_confirmed", "long_context", "plan"]
    message: str = Field(min_length=1, max_length=2000)
    skill_id: str | None = None


def load_cases(path: Path | None = None) -> list[TaskCase]:
    lines = (path or EVAL_ROOT / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
    cases = [TaskCase.model_validate_json(line) for line in lines if line.strip()]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("任务编号必须唯一，评估集不能为空。")
    return cases


def load_vehicles() -> list[dict]:
    return json.loads((EVAL_ROOT / "fixtures" / "vehicles.json").read_text(encoding="utf-8"))
