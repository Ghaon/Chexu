import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from src.evaluation.catalog import EVAL_ROOT, load_cases, load_vehicles
from src.evaluation.grading import grade
from src.evaluation.metrics import ModelBudget, TraceCollector, delta, summarize


class EvaluationSetupError(Exception):
    pass


def price(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("单价必须为非负有限数，单位为美元/百万 token。")
    return result


def parser():
    result = argparse.ArgumentParser(description="Run real Chexu tasks in isolated PostgreSQL schemas; model API calls may incur fees.")
    result.add_argument("--list", action="store_true", help="仅列出任务，不连接数据库或模型")
    result.add_argument("--case", action="append", dest="case_ids", help="只运行指定任务，可重复传入")
    result.add_argument("--limit", type=int, default=9, help="最多运行前 N 项，默认全部九项")
    result.add_argument("--max-model-calls", type=int, default=40, help="整次评估的真实模型调用上限")
    result.add_argument("--input-price-per-million", type=price)
    result.add_argument("--output-price-per-million", type=price)
    return result


def provision_users(client, nonce, index, password, stored_hash):
    from src.database.connection import SessionFactory
    from src.database.models import EmployeeRecord
    people, headers = {}, {}
    with SessionFactory.begin() as session:
        for suffix, role in (("s", "sales"), ("m", "manager")):
            record = EmployeeRecord(username=f"eval_{nonce}_{index}_{suffix}", role=role, password_hash=stored_hash)
            session.add(record)
            session.flush()
            people[role] = (record.employee_id, record.username)
    for role, (_, username) in people.items():
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        if response.status_code != 200:
            raise EvaluationSetupError("evaluation_login_failed")
        headers[role] = {"Authorization": "Bearer " + response.json()["access_token"]}
    return people["sales"][0], headers


def execute_cases(cases, budget, nonce, input_price=None, output_price=None):
    from fastapi.testclient import TestClient
    from src.agent import vehicle_agent
    from src.agent.tracing import logger
    from src.api_view.app import app
    from src.database.connection import SessionFactory
    from src.database.models import VehicleRecord
    from src.evaluation.scenarios import CaseEnvironment, CaseExecutionError
    from src.security import hash_password

    fixtures = load_vehicles()
    with SessionFactory.begin() as session:
        session.add_all(VehicleRecord(**item) for item in fixtures)
    password = secrets.token_urlsafe(24)
    stored_hash = hash_password(password)
    original_model, original_handlers = vehicle_agent.tool_model, list(logger.handlers)
    collector = TraceCollector()
    logger.handlers = [collector]
    # 保持同一个真实 ChatOpenAI 客户端，只追加整次评估的请求预算回调。
    vehicle_agent.tool_model = original_model.with_config(callbacks=[budget])
    results = []
    try:
        with TestClient(app) as client:
            for index, case in enumerate(cases):
                if budget.calls >= budget.limit:
                    results.append({"id": case.id, "title": case.title, "status": "not_run_budget",
                                    "duration_seconds": 0, "checks": [], "metrics": {}, "artifacts": {}})
                    continue
                owner, headers = provision_users(client, nonce, index, password, stored_hash)
                environment = CaseEnvironment(client, owner, headers)
                before, event_start, start = budget.snapshot(), len(collector.events), perf_counter()
                plan_start = len(budget.planning_outputs)
                error_code = None
                try:
                    environment.run(case)
                except CaseExecutionError as exc:
                    error_code = str(exc)
                except Exception as exc:
                    error_code = type(exc).__name__
                duration = round(perf_counter() - start, 3)
                events = collector.events[event_start:]
                artifacts = environment.artifacts
                artifacts["tool_names"] = [event["tool_name"] for event in events if event.get("event") == "tool.start"]
                artifacts["permission_rejections"] = sum(event.get("event") == "tool.batch.rejected" for event in events)
                artifacts["error_types"] = sorted({event["error_type"] for event in events if event.get("error_type")})
                artifacts["planning_outputs"] = budget.planning_outputs[plan_start:]
                checks = grade(case, artifacts, fixtures)
                if error_code:
                    checks.append({"name": "任务执行无运行错误", "passed": False})
                result = {"id": case.id, "title": case.title,
                    "status": "error" if error_code else "passed" if all(item["passed"] for item in checks) else "failed",
                    "duration_seconds": duration, "checks": checks, "error_code": error_code,
                    "metrics": delta(budget.snapshot(), before), "artifacts": artifacts,
                    "manual_review": (["核对自然语言结论与依据的语义关系；自动检查不证明完整语义正确。"]
                                      if case.scenario in {"report", "compare"} else [])}
                results.append(result)
                print(f"{case.id}: {result['status']} ({duration}s, {result['metrics']['model_calls']} model calls)", flush=True)
    finally:
        vehicle_agent.tool_model = original_model
        logger.handlers = original_handlers
    summary = summarize(results, input_price, output_price)
    citations = [item["artifacts"]["citation_metrics"] for item in results if item["artifacts"].get("citation_metrics")]
    valid, total = sum(item["valid"] for item in citations), sum(item["total"] for item in citations)
    summary["citation_integrity"] = {"valid": valid, "total": total, "rate": valid / total if total else None}
    return results, summary


def write_report(folder: Path, report: dict):
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report.get("summary", {})
    lines = ["# Chexu 真实任务评估", "", f"模型：{report['model']}；项目：{report['version']}；时间：{report['started_at']}",
             "", "真实模型 + 真实后端接口 + 隔离模拟资料。结果仅覆盖本评估集，规则通过不等于全部语义正确。",
             "", "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```", "",
             "| 任务 | 结果 | 耗时/秒 | 模型调用 |", "| --- | --- | ---: | ---: |"]
    for item in report.get("results", []):
        lines.append(f"| {item['title']} | {item['status']} | {item['duration_seconds']} | {item['metrics'].get('model_calls', 0)} |")
    for item in report.get("results", []):
        lines.extend(["", f"## {item['title']}", ""])
        lines.extend(f"- {'通过' if check['passed'] else '未通过'}：{check['name']}" for check in item["checks"])
        lines.extend(f"- 待人工复核：{note}" for note in item.get("manual_review", []))
        if item.get("error_code"):
            lines.append(f"- 运行错误：{item['error_code']}")
        for phase, response in enumerate(item["artifacts"].get("responses", []), 1):
            lines.extend(["", f"阶段 {phase} 的回答：", "", response.get("reply", "")])
    lines.extend(["", "## 隔离与限制", "", json.dumps(report.get("isolation", {}), ensure_ascii=False), "",
                  "选车和价格按结构化库存及回复检查；引用检查来源、哈希与逐字摘录。引用正确仍需人工核对陈述是否得到原文支持。",
                  "长对话历史是合成夹具；人工审批动作由脚本使用隔离经理账号发起。未报告 usage 的请求不按零消耗计算。"])
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    arg_parser = parser()
    args = arg_parser.parse_args(argv)
    cases = load_cases()
    if args.case_ids:
        if set(args.case_ids) - {case.id for case in cases}:
            arg_parser.error("存在未知任务编号，请使用 --list 查看。")
        cases = [case for case in cases if case.id in args.case_ids]
    if not 1 <= args.limit <= 100 or not 1 <= args.max_model_calls <= 200:
        arg_parser.error("limit 范围为 1～100，max-model-calls 范围为 1～200。")
    cases = cases[:args.limit]
    if args.list:
        for case in cases:
            print(f"{case.id}: {case.title}")
        return 0
    if (args.input_price_per_million is None) != (args.output_price_per_million is None):
        arg_parser.error("输入和输出单价需同时提供。")
    if "src.database.connection" in sys.modules:
        raise RuntimeError("请使用 scripts/evaluate_tasks.py 在独立进程运行评估。")

    from psycopg import Connection, sql
    from sqlalchemy.engine import make_url
    from src.agent.config import PROJECT_ROOT, Settings, get_settings
    from src.version import VERSION

    initial = Settings()
    if (".example" in initial.model_base_url or initial.model_name in {"your-tool-calling-model", "test-model"}
            or initial.model_api_key.get_secret_value() in {"replace-with-your-key", "test-key"}):
        print("请先在 .env 配置可用的模型。未发送模型请求。")
        return 2
    nonce = uuid4().hex[:12]
    schema, checkpoint_schema = f"chexu_eval_{nonce}", f"chexu_eval_{nonce}_agent"
    conninfo = make_url(initial.database_url.get_secret_value()).set(drivername="postgresql").render_as_string(hide_password=False)
    root = PROJECT_ROOT / "data" / "evals"
    root.mkdir(parents=True, exist_ok=True)
    report = {"run_id": nonce, "started_at": datetime.now(timezone.utc).isoformat(), "version": VERSION,
              "model": initial.model_name, "model_kind": "live", "model_call_limit": args.max_model_calls,
              "source_sha256": sha256(b"".join(path.relative_to(PROJECT_ROOT).as_posix().encode() + b"\0" + path.read_bytes()
                  for path in sorted([*(PROJECT_ROOT / "src").rglob("*.py"),
                                      *(PROJECT_ROOT / "skills").rglob("*.md"),
                                      *(PROJECT_ROOT / "skills").rglob("*.toml")]))).hexdigest(),
              "dataset_sha256": sha256(b"".join(path.read_bytes() for path in [EVAL_ROOT / "tasks.jsonl",
                  EVAL_ROOT / "fixtures" / "vehicles.json", EVAL_ROOT / "fixtures" / "report.txt"])).hexdigest(),
              "results": [], "isolation": {"business_schema": schema, "checkpoint_schema": checkpoint_schema,
                                           "cleanup_completed": False}}
    created = []
    setup_error = None
    budget = ModelBudget(args.max_model_calls)
    try:
        with Connection.connect(conninfo, autocommit=True, connect_timeout=5,
                                options="-c statement_timeout=5000 -c lock_timeout=2000") as connection:
            for name in (schema, checkpoint_schema):
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
                created.append(name)
        with TemporaryDirectory(prefix=f"work_{nonce}_", dir=root) as workspace:
            os.environ.update(CHEXU_DATABASE_SCHEMA=schema, CHEXU_CHECKPOINT_SCHEMA=checkpoint_schema,
                              CHEXU_REPORT_STORAGE_DIR=str(Path(workspace) / "reports"), CHEXU_TASK_WORKER_ENABLED="false")
            get_settings.cache_clear()
            migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT,
                                      capture_output=True, timeout=60)
            if migrated.returncode:
                raise EvaluationSetupError("migration_failed")
            from src.database.checkpoints import main as setup_checkpoints
            with redirect_stdout(io.StringIO()):
                if setup_checkpoints():
                    raise EvaluationSetupError("checkpoint_setup_failed")
            report["results"], report["summary"] = execute_cases(cases, budget, nonce,
                args.input_price_per_million, args.output_price_per_million)
    except Exception as exc:
        setup_error = str(exc) if isinstance(exc, EvaluationSetupError) else type(exc).__name__
        report["setup_error"] = setup_error
    finally:
        if "src.database.connection" in sys.modules:
            sys.modules["src.database.connection"].engine.dispose()
        try:
            if created:
                with Connection.connect(conninfo, autocommit=True, connect_timeout=5,
                                        options="-c statement_timeout=10000 -c lock_timeout=2000") as connection:
                    for name in reversed(created):
                        # 仅清理本进程成功创建的随机 schema，禁止接收用户提供的删除目标。
                        if name not in {schema, checkpoint_schema} or not re.fullmatch(r"chexu_eval_[0-9a-f]{12}(?:_agent)?", name):
                            raise EvaluationSetupError("invalid_cleanup_target")
                        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
            report["isolation"]["cleanup_completed"] = True
        except Exception as exc:
            report["isolation"]["cleanup_error"] = type(exc).__name__
    folder = root / f"result_{nonce}"
    write_report(folder, report)
    print(f"Report: {folder / 'report.md'}", flush=True)
    if setup_error:
        print(f"Evaluation setup failed: {setup_error}")
        return 2
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    return 0 if report["summary"].get("passed") == len(cases) and report["isolation"]["cleanup_completed"] else 1
