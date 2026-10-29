"""离线评估技能路由规则，不访问模型和数据库。"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.config import PROJECT_ROOT
from src.agent.skills import get_skill_registry


def main() -> int:
    registry = get_skill_registry()
    failures = []
    cases = [json.loads(line) for line in (PROJECT_ROOT / "evals/skill_routing.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    for index, case in enumerate(cases, 1):
        skill = registry.select(case["message"], "sales", explicit=case.get("explicit"), previous=case.get("previous"))
        if skill.id != case["expected"]:
            failures.append({"case": index, "expected": case["expected"], "actual": skill.id})
    print(json.dumps({"total": len(cases), "passed": len(cases) - len(failures), "failures": failures}, ensure_ascii=False))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
