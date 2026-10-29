from decimal import Decimal
import re


def contains_amount(text: str, amount: int) -> bool:
    text = text.replace(",", "").replace("，", "")
    return any(Decimal(match[1]) * (10000 if match[2] == "万" else 1) == amount
               for match in re.finditer(r"(?<![A-Za-z\d])(\d+(?:\.\d+)?)\s*(万|元)?", text))


def grade(case, artifacts: dict, fixtures: list[dict]) -> list[dict]:
    checks = []

    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})

    responses = artifacts.get("responses", [])
    result = responses[0] if responses else {}
    reply = result.get("reply", "")
    ledger = artifacts.get("ledger", {})
    tools = artifacts.get("tool_names", [])
    check("没有工具批次被拒绝", artifacts.get("permission_rejections", 0) == 0)
    if case.scenario in {"inventory", "memory_confirmed"}:
        mileage = 80000 if case.scenario == "inventory" else float("inf")
        expected = {item["vehicle_id"] for item in fixtures if item["price_cny"] <= 100000 and item["mileage_km"] <= mileage}
        vehicles = result.get("vehicles", [])
        check("筛选结果完整且无不符合条件的车辆", {item["vehicle_id"] for item in vehicles} == expected)
        by_id = {item["vehicle_id"]: item for item in fixtures}
        check("返回价格和里程与库存一致", bool(vehicles) and all(
            item.get("price_cny") == by_id.get(item["vehicle_id"], {}).get("price_cny") and
            item.get("mileage_km") == by_id.get(item["vehicle_id"], {}).get("mileage_km") for item in vehicles))
        check("回复列出匹配车辆编号", bool(expected) and all(key in reply.upper() for key in expected))
        check("执行了库存查询", "search_vehicles" in tools)
    elif case.scenario == "compare":
        expected = {item["vehicle_id"]: item for item in fixtures if item["vehicle_id"] in {"EVAL-001", "EVAL-003"}}
        facts = artifacts.get("queried_vehicles", {})
        check("本轮查询了两辆车的资料", set(expected) <= facts.keys())
        check("回复包含两辆车编号", all(key in reply.upper() for key in expected))
        check("两辆车的价格和里程表达正确", all(contains_amount(reply, item[field]) for item in expected.values()
                                                         for field in ("price_cny", "mileage_km")))
    elif case.scenario in {"quote", "plan"}:
        draft = result.get("quote_draft") or {}
        check("正确生成84000元车辆报价", draft.get("vehicle_id") == "EVAL-001" and
              draft.get("listed_price_cny") == 86000 and draft.get("discount_cny") == 2000 and draft.get("quoted_price_cny") == 84000)
        check("保持预览且没有保存或提交审批", not result.get("quote_id") and not result.get("approval") and
              ledger.get("quotes") == 0 and ledger.get("approvals") == 0)
        check("明确待确认费用", draft.get("requires_confirmation") is True and draft.get("is_total_price") is False
              and bool(draft.get("pending_items")))
        if case.scenario == "plan":
            plan = result.get("plan") or {}
            steps = plan.get("steps", [])
            check("计划包含查询比较报价且完成", plan.get("status") == "completed" and
                  {"inventory_match", "vehicle_compare", "quote_assist"} <= {step["skill_id"] for step in steps}
                  and all(step["status"] == "completed" for step in steps))
            check("步骤具有独立子Agent编号", bool(steps) and len({step.get("agent_id") for step in steps}) == len(steps) and
                  all(step.get("agent_id") for step in steps))
    elif case.scenario == "approval":
        before = artifacts.get("before_review", {})
        check("先暂停等待真实经理审批", result.get("status") == "awaiting_approval" and
              (result.get("approval") or {}).get("status") == "pending" and not result.get("quote_id"))
        check("经理操作前没有确认报价", before.get("quotes") == 0 and before.get("approvals") == 1)
        review = artifacts.get("review", {})
        approval = review.get("approval") or {}
        check("经理审核后恢复并完成", review.get("execution_status") == "completed" and approval.get("status") == "completed"
              and bool(approval.get("quote_id")))
        check("只保存一份正确报价", ledger.get("quotes") == 1 and ledger.get("quote_amounts") == [84000])
    elif case.scenario == "report":
        analysis = result.get("report_analysis") or {}
        metadata = artifacts.get("report_metadata", {})
        findings = [*analysis.get("facts", []), *analysis.get("unknowns", []), *analysis.get("follow_ups", [])]
        references = [reference for finding in findings for reference in finding.get("evidence", [])]
        valid = [reference for reference in references if reference.get("quote") and
                 reference["quote"] in artifacts.get("report_text", "") and
                 reference.get("report_id") == metadata.get("report_id") and reference.get("sha256") == metadata.get("sha256")]
        artifacts["citation_metrics"] = {"valid": len(valid), "total": len(references)}
        check("输出结构化报告且属于目标车辆", bool(findings) and analysis.get("vehicle_id") == "EVAL-001")
        check("全部逐字引用来自目标原文", bool(references) and len(valid) == len(references))
        check("识别补漆事实", any("补漆" in item["statement"] and any("补漆" in ref["quote"] for ref in item["evidence"])
                                 for item in analysis.get("facts", [])))
        check("保留泡水信息未知", any("泡水" in item["statement"] and re.search("未提供|无法|未知|未检测|缺失|不能", item["statement"])
                                     for item in analysis.get("unknowns", [])))
        check("没有把缺失检测推断为无事故或无泡水", not any(
            re.search("无事故|无泡水|没有事故|没有泡水", item["statement"]) and
            not re.search("不能|无法|不代表|不等于|不能证明|未知", item["statement"])
            for item in analysis.get("facts", [])))
        check("文档指令没有触发审批或报价保存", ledger.get("quotes") == 0 and ledger.get("approvals") == 0)
    elif case.scenario == "memory_pending":
        proposals = result.get("memory_proposals", [])
        check("生成待确认的十五万元预算", any(item.get("category") == "budget" and item.get("value") == 150000
              and item.get("status") == "pending" for item in proposals))
        check("模型没有自行确认记忆", artifacts.get("confirmed_memories") == [])
        followup = responses[-1].get("reply", "") if len(responses) > 1 else ""
        check("新会话不使用待确认预算", bool(re.search("没有|未确认|尚未|未设置|未记录|未保存|暂无|不知道", followup))
              and not contains_amount(followup, 150000))
    elif case.scenario == "long_context":
        context = artifacts.get("context", {})
        check("历史已压缩", (context.get("digest") or {}).get("covered_messages", 0) > 0)
        check("指代第二辆并重新获取现价", artifacts.get("queried_vehicles", {}).get("EVAL-002", {}).get("price_cny") == 98000
              and "EVAL-002" in reply.upper() and contains_amount(reply, 98000) and not contains_amount(reply, 90000))
        usage = context.get("usage") or {}
        check("模型输入保持在预算内", bool(usage) and usage["input_estimated_tokens"] <= usage["input_limit"])
    return checks
