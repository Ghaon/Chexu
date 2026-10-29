from dataclasses import replace
import json
import re
from uuid import uuid5

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import APITimeoutError
from pydantic import ValidationError

from src.agent.config import get_settings
from src.agent.context import AgentContext
from src.agent.conversation_context import prepare_context
from src.agent.skills import get_skill_registry, SkillError
from src.agent.tracing import emit_context_event
from src.domain.execution import ExecutionTimedOut
from src.domain.planning import PlanInput
from src.services import memories


class PlanError(SkillError):
    pass


def approval_authorized(message: str, previous: bool = False) -> bool:
    if re.search(r"(?:不要|无需|不需要|别).{0,8}(?:审批|审核)", message):
        return False
    return previous or bool(re.search(r"(?:提交|申请|发起).{0,8}(?:审批|审核)", message))


def generate_plan(state: dict, config: dict, context: AgentContext, model, *, previous: dict | None = None) -> dict:
    settings, registry = get_settings(), get_skill_registry()
    catalog = [{"id": item.id, "description": item.description} for item in registry.list_metadata(context.role)
               if item.id in {"inventory_match", "vehicle_report", "vehicle_compare", "quote_assist", "web_research"} and item.tools]
    completed = [step for step in (previous or {}).get("steps", []) if step["status"] == "completed"]
    available = settings.plan_max_steps - len(completed)
    prompt = (context.skill.instructions + f"\n最多新增 {available} 步。只规划用户确实要求的工作，报价步骤最多一次且放在最后。"
              "每步 goal 和 inputs 必须包含执行所需的具体条件。depends_on 是本次新步骤的1起始编号；"
              "只能依赖较早步骤。省略表示顺序执行，空列表表示独立，可并行只读步骤。"
              "updates 按时间排列，最新用户调整覆盖冲突的旧条件。已完成步骤不要重复规划。"
              "技能名不是可调用函数，只能填写在 steps[].skill_id。必须单次调用 submit_task_plan，"
              "以 steps 数组提交全部步骤，不得直接调用 inventory_match 等技能名。"
              "使用 submit_task_plan 提交，不能执行计划以外的工具。可用技能：" + json.dumps(catalog, ensure_ascii=False))
    prompt += memories.confirmed_context(context.employee_id)
    try:
        if previous is not None:
            history = [HumanMessage(content=json.dumps({"objective": previous["objective"], "updates": previous.get("updates", []),
                "completed": [{"id": step["id"], "skill_id": step["skill_id"], "goal": step["goal"],
                               "summary": (step.get("result") or {}).get("reply", "")[:700]} for step in completed],
                "blocked": [{"skill_id": step["skill_id"], "goal": step["goal"],
                             "reason": (step.get("result") or {}).get("reply", "")[:700]}
                            for step in previous["steps"] if step["status"] in {"blocked", "needs_input", "running"}]}, ensure_ascii=False))]
        tools = [{"type": "function", "function": {"name": "submit_task_plan", "description": "提交顺序执行的技能计划。",
                                                   "parameters": PlanInput.model_json_schema()}}]
        prepared = prepare_context({"messages": history} if previous is not None else state, prompt, tools, settings)
        if sum(len(str(message.content)) for message in prepared.messages) > settings.subagent_context_max_chars:
            raise PlanError("规划上下文超过容量限制，请缩小任务范围或开启新会话。")
        context.control.charge("model_calls", 1, context.budget.max_model_calls)
        context.budget.consume_model()
        response = model.invoke(prepared.messages, config=config, tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_task_plan"}}, parallel_tool_calls=False,
            timeout=context.control.timeout(settings.model_timeout_seconds), max_tokens=settings.context_output_tokens)
    except APITimeoutError as exc:
        raise ExecutionTimedOut("任务规划请求已超时，可从运行记录恢复。") from exc
    finally:
        context.control.check()
    if not isinstance(response, AIMessage) or len(response.tool_calls) != 1 or response.tool_calls[0]["name"] != "submit_task_plan":
        raise PlanError("规划模型必须通过 submit_task_plan 返回结构化步骤，请恢复执行后重试。")
    try:
        draft = PlanInput.model_validate(response.tool_calls[0]["args"])
    except ValidationError as exc:
        raise PlanError("任务计划格式无效：只能选择已提供的执行技能，并填写每一步目标。") from exc
    if len(draft.steps) > available or any(not step.goal.strip() for step in draft.steps):
        raise PlanError("计划步骤过多或目标为空，请缩小任务范围。")
    quoted = [index for index, step in enumerate(draft.steps) if step.skill_id == "quote_assist"]
    if quoted and quoted != [len(draft.steps) - 1]:
        raise PlanError("报价技能最多执行一次，并且必须放在最后。")
    allowed = {item["id"] for item in catalog}
    if any(step.skill_id not in allowed for step in draft.steps):
        raise PlanError("计划包含当前身份不可用的技能。")
    # 只为本次选中的技能加载并固定正文，恢复时继续校验当前权限。
    def snapshot(skill_id):
        skill = registry.get(skill_id, context.role)
        return skill.model_copy(update={"max_model_calls": min(skill.max_model_calls, settings.max_model_calls),
                                        "max_tool_calls": min(skill.max_tool_calls, settings.max_tool_calls)}).model_dump(mode="json")

    revision = previous["revision"] if previous else 1
    steps = []
    for index, step in enumerate(draft.steps):
        dependencies = step.depends_on if step.depends_on is not None else ([index] if index else [])
        if len(set(dependencies)) != len(dependencies) or any(number < 1 or number > index for number in dependencies):
            raise PlanError("步骤依赖无效：只能引用本次计划中较早的步骤，且不能重复。")
        depends_on = [steps[number - 1]["id"] for number in dependencies]
        if completed:
            depends_on.extend(item["id"] for item in completed)
        if step.skill_id == "quote_assist":
            depends_on = [item["id"] for item in [*completed, *steps]]
        step_id = f"r{revision}_step_{index + 1}" if revision > 1 else f"step_{index + 1}"
        steps.append({"id": step_id, "skill_id": step.skill_id, "goal": step.goal.strip(), "status": "pending",
                      "result": None, "snapshot": snapshot(step.skill_id), "depends_on": depends_on,
                      "inputs": step.inputs.model_dump(mode="json", exclude_none=True),
                      "agent_id": str(uuid5(context.turn_id, step_id))})
    message = next(item.content for item in reversed(state["messages"]) if isinstance(item, HumanMessage))
    plan = {**(previous or {}), "engine": 2, "turn_id": str(context.turn_id), "revision": revision, "status": "running", "current_step": None,
            "active_steps": [], "steps": [*completed, *steps],
            "objective": (previous or {}).get("objective", message), "updates": (previous or {}).get("updates", []),
            "replan_count": (previous or {}).get("replan_count", 0),
            "max_replans": (previous or {}).get("max_replans", settings.plan_max_replans),
            "max_parallel": (previous or {}).get("max_parallel", settings.plan_max_parallel),
            "allow_approval": (previous or {}).get("allow_approval", approval_authorized(message))}
    emit_context_event("plan.replanned" if previous else "plan.created", context,
                       step_count=len(plan["steps"]), revision=revision, status="running")
    # 重规划的合成输入不是原始聊天历史，不能用其游标覆盖会话摘要。
    context_update = prepared.state_update() if previous is None else {"context_usage": prepared.usage.model_dump()}
    return {**context_update, "plan": plan, "model_calls_used": context.budget.model_calls,
            "messages": [response, ToolMessage(content="计划已通过检查，将按顺序执行。", name="submit_task_plan",
                                               tool_call_id=response.tool_calls[0]["id"])]}


def active_context(state: dict, context: AgentContext) -> AgentContext:
    if context.skill.id != "deal_review" or not state.get("plan") or state["plan"].get("current_step") is None:
        return context
    plan = state["plan"]
    step = plan["steps"][plan["current_step"] - 1]
    skill = get_skill_registry().restore(step["snapshot"], context.role)
    if not plan["allow_approval"]:
        skill = skill.model_copy(update={"tools": tuple(name for name in skill.tools if name != "request_quote_approval")})
    settings = get_settings()
    control = replace(context.control, step_id=step["id"],
                      step_model_limit=min(skill.max_model_calls, settings.max_model_calls),
                      step_tool_limit=min(skill.max_tool_calls, settings.max_tool_calls))
    return replace(context, skill=skill, control=control)


def start_step(state: dict, context: AgentContext) -> dict:
    context.control.check()
    plan = state["plan"]
    index = next(index for index, step in enumerate(plan["steps"]) if step["status"] == "pending")
    steps = [dict(step) for step in plan["steps"]]
    steps[index]["status"] = "running"
    updated = {**plan, "current_step": index + 1, "steps": steps}
    active = active_context({"plan": updated}, context)
    emit_context_event("plan.step.started", active, step_id=steps[index]["id"], step_index=index + 1,
                       step_count=len(steps), status="running")
    return {"plan": updated, "step_message_start": len(state["messages"]), "report_analysis": None,
            "vehicle": None, "vehicles": [], "has_more": False, "quote_draft": None,
            "reply": "", "execution_status": None}


def complete_step(state: dict, result: dict, context: AgentContext) -> dict:
    plan, active = state["plan"], active_context(state, context)
    index = plan["current_step"] - 1
    recent = state["messages"][state["step_message_start"]:]
    status = result_status(recent, result, active.skill.id)
    if status == "blocked":
        status = "needs_input"
    steps = [dict(step) for step in plan["steps"]]
    fields = ("reply", "vehicle", "vehicles", "has_more", "quote_draft", "approval", "quote_id", "report_sources", "report_analysis", "web_sources", "web_search_status")
    steps[index].update(status=status, result={key: result.get(key) for key in fields})
    if status == "needs_input":
        steps = [{**step, "status": "skipped"} if step["status"] == "pending" else step for step in steps]
    updated = {**plan, "steps": steps, "status": "needs_input" if status == "needs_input" else "running"}
    emit_context_event(f"plan.step.{status}", active, step_id=steps[index]["id"], step_index=index + 1,
                       step_count=len(steps), status=status)
    return {**result, "plan": updated, "execution_status": None}


def result_status(messages, result, skill_id):
    successful = [item for item in messages if isinstance(item, ToolMessage) and item.status != "error"]
    if skill_id == "inventory_match":
        sufficient = bool(result.get("vehicle") or result.get("vehicles"))
    elif skill_id == "vehicle_report":
        sufficient = result.get("report_analysis") is not None
    elif skill_id == "quote_assist":
        sufficient = result.get("quote_draft") is not None
    elif skill_id == "web_research":
        sufficient = bool(result.get("web_sources")) and result.get("web_search_status") == "completed"
    else:
        vehicles = set()
        for item in successful:
            if item.name not in {"get_vehicle", "search_vehicles"}:
                continue
            data = json.loads(item.content)
            vehicles.update(vehicle["vehicle_id"] for vehicle in data.get("vehicles", [data]) if "vehicle_id" in vehicle)
        sufficient = len(vehicles) >= 2
    if sufficient and successful:
        return "completed"
    return "blocked" if any(isinstance(item, ToolMessage) and item.status == "error"
        and item.additional_kwargs.get("error_code") != "tool_permission_denied" for item in messages) else "needs_input"


def summarize_plan(state: dict, context: AgentContext) -> dict:
    context.control.check()
    plan = state["plan"]
    status = "needs_input" if plan["status"] == "needs_input" else "completed"
    results = [step["result"] for step in plan["steps"] if step["result"] is not None]
    reply = "\n\n".join(f"{index + 1}. {step['goal']}\n{step['result']['reply']}"
                         for index, step in enumerate(plan["steps"]) if step["result"] is not None)
    if status == "needs_input":
        reply += "\n\n当前步骤尚未取得足够资料，后续步骤未执行。请通过计划续接接口补充条件或资料。"
    if plan.get("adjustments"):
        reply += "\n\n计划调整记录：\n" + "\n".join(plan["adjustments"])
    combined = {}
    for key in ("vehicle", "vehicles", "quote_draft", "approval", "quote_id", "report_analysis"):
        combined[key] = next((item[key] for item in reversed(results) if item.get(key)), [] if key == "vehicles" else None)
    sources = {(item["report_id"], item["chunk"]): item for result in results for item in result.get("report_sources") or []}
    web_sources = {item["url"]: item for result in results for item in result.get("web_sources") or []}
    web_states = [item["web_search_status"] for item in results if item.get("web_search_status")]
    web_status = ("partial" if web_sources else "failed") if any(item in {"partial", "failed", "empty"} for item in web_states) else "completed" if web_states else None
    emit_context_event(f"plan.{status}", context, step_count=len(plan["steps"]), status=status)
    return {**combined, "has_more": bool(results and results[-1].get("has_more")), "report_sources": list(sources.values())[:30],
            "web_sources": list(web_sources.values())[:10], "web_search_status": web_status,
            "plan": {**plan, "status": status}, "reply": reply, "execution_status": "completed",
            "messages": [AIMessage(content=reply, response_metadata={"generated_by": "plan_result_renderer"})]}
