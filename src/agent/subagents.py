from dataclasses import replace
import json
from uuid import UUID

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition
from openai import APITimeoutError

from src.agent.config import get_settings
from src.agent.middleware import ToolExecutionMiddleware
from src.agent.planning import result_status
from src.agent.report_analysis import collect_report_evidence
from src.agent.skills import get_skill_registry
from src.agent.tool_policies import vehicle_tool_policy
from src.agent.tools.registry import AGENT_TOOLS, READ_ONLY_TOOLS
from src.agent.tracing import emit_context_event
from src.domain.delegation import AgentOutput, SubagentContextExceeded, SubagentResult
from src.domain.execution import ExecutionTimedOut


def merge_child_results(left: dict, right: dict) -> dict:
    if right.get("__reset__"):
        return {}
    return {**left, **right}


def delegation_context(delegation: dict, root):
    skill = get_skill_registry().restore(delegation["snapshot"], root.role)
    if not delegation["allow_approval"]:
        skill = skill.model_copy(update={"tools": tuple(name for name in skill.tools if name != "request_quote_approval")})
    settings = get_settings()
    control = replace(root.control, step_id=delegation["step_id"], step_model_limit=min(skill.max_model_calls, settings.max_model_calls),
                      step_tool_limit=min(skill.max_tool_calls, settings.max_tool_calls))
    return replace(root, skill=skill, control=control, agent_id=UUID(delegation["agent_id"]))


def make_handoff(plan: dict, step: dict) -> dict:
    # 只交接声明依赖的结构化资料；不交接父 Agent 或兄弟 Agent 的消息历史。
    required = set(step.get("depends_on", []))
    while True:
        expanded = required | {ancestor for item in plan["steps"] if item["id"] in required for ancestor in item.get("depends_on", [])}
        if expanded == required:
            break
        required = expanded
    dependencies = [item for item in plan["steps"] if item["id"] in required and item.get("result")]
    vehicles, analyses, recommendations, web_sources = {}, [], [], {}
    for dependency in dependencies:
        result = dependency["result"]
        for source in result.get("web_sources") or []:
            web_sources[source["url"]] = source
        for vehicle in [result.get("vehicle"), *(result.get("vehicles") or [])]:
            if vehicle:
                vehicles[vehicle["vehicle_id"]] = vehicle
        if step["skill_id"] == "vehicle_compare" and result.get("report_analysis"):
            analyses.append(result["report_analysis"])
        if step["skill_id"] == "quote_assist" and dependency["skill_id"] == "vehicle_compare":
            recommendations.append({"step_id": dependency["id"], "summary": result["reply"][:700]})
    selected = step.get("inputs", {}).get("vehicle_ids", [])
    if selected:
        vehicles = {key: value for key, value in vehicles.items() if key in selected}
        analyses = [item for item in analyses if item["vehicle_id"] in selected]
    packet = {"goal": step["goal"], "inputs": step.get("inputs", {}),
              "clarifications": plan.get("updates", []), "vehicles": list(vehicles.values())[:6],
              "report_analyses": analyses[:2], "warnings": [], "untrusted_input": True}
    packet["recommendations"] = recommendations[:2]
    packet["web_sources"] = list(web_sources.values())[:5]
    if web_sources:
        packet["warnings"].append("联网来源仅为公开网页参考，不是门店库存事实、检测证据或真实成交价。")
    limit = get_settings().subagent_handoff_max_chars
    if len(json.dumps(packet, ensure_ascii=False)) > limit and packet["web_sources"]:
        packet["web_sources"] = []
        packet["warnings"].append("网页来源超出交接容量，未传递；不得假设网页内容。")
    if len(json.dumps(packet, ensure_ascii=False)) > limit:
        packet["report_analyses"] = []
        packet["warnings"].append("报告分析超出交接容量，未传递；需要时重新查询，不得假设报告结论。")
    if len(json.dumps(packet, ensure_ascii=False)) > limit:
        raise SubagentContextExceeded("交接资料超出容量，请缩小目标或补充信息的长度。")
    return packet


class SubagentState(MessagesState):
    delegation: dict
    vehicle: dict | None
    vehicles: list[dict]
    has_more: bool
    quote_draft: dict | None
    reply: str
    approval: dict | None
    quote_id: str | None
    report_sources: list[dict]
    web_sources: list[dict]
    web_search_status: str | None
    report_analysis: dict | None
    execution_status: str | None
    outcome: dict
    context_digest: dict
    context_usage: dict


def build_subagent(root, call_model, finish, approval_node, compact_context):
    def scoped(state, config):
        context = delegation_context(state["delegation"], root)
        metadata = {**config.get("metadata", {}), "skill_id": context.skill.id, "skill_version": context.skill.version,
                    "step_id": context.control.step_id, "agent_id": str(context.agent_id), "parent_agent_id": str(root.turn_id)}
        return context, {**config, "metadata": metadata}

    def model_node(state: SubagentState, config: RunnableConfig):
        context, config = scoped(state, config)
        context.control.charge("model_calls", 1, root.budget.max_model_calls)
        root.budget.consume_model()
        try:
            return call_model(state, config, context)
        except APITimeoutError as exc:
            raise ExecutionTimedOut("子任务模型请求超时，可从运行记录恢复。") from exc
        finally:
            context.control.check()

    def tool_node(state: SubagentState, config: RunnableConfig):
        context, config = scoped(state, config)
        return ToolExecutionMiddleware(AGENT_TOOLS, context, root.budget).invoke(state, config, policy=vehicle_tool_policy)

    def approval(state: SubagentState, config: RunnableConfig):
        context, config = scoped(state, config)
        bounded = {**config, "configurable": {**config.get("configurable", {}),
            "tool_timeout_ms": max(1, int(context.control.timeout(get_settings().tool_timeout_seconds) * 1000))}}
        result = approval_node(state, bounded)
        context.control.check()
        return result

    def after_model(state):
        if tools_condition(state) != END:
            return "tools"
        context = delegation_context(state["delegation"], root)
        if "finalize_vehicle_report" in context.skill.tools and collect_report_evidence(state["messages"])[0]:
            return "report_guard"
        return "finish"

    def after_tools(state):
        if state.get("report_analysis") is not None and isinstance(state["messages"][-1], AIMessage):
            return "finish"
        last = state["messages"][-1]
        return "approval" if last.name == "request_quote_approval" and last.status != "error" else "prepare_context"

    def report_guard(state):
        last = state["messages"][-1]
        return {"messages": [AIMessage(content="正在校验报告原文引用。", id=last.id),
            SystemMessage(content="报告已经读取，请单独调用 finalize_vehicle_report，逐项提交本子任务实际读取的原文引用。") ]}

    def finalize(state):
        context = delegation_context(state["delegation"], root)
        context.control.check()
        result = finish(state)
        output = AgentOutput.model_validate({key: result.get(key) for key in AgentOutput.model_fields})
        outcome = SubagentResult(agent_id=context.agent_id, step_id=context.control.step_id, skill_id=context.skill.id,
            status=result_status(state["messages"], result, context.skill.id), output=output)
        return {"outcome": outcome.model_dump(mode="json")}

    builder = StateGraph(SubagentState)
    builder.add_node("prepare_context", lambda state, config: compact_context(state, scoped(state, config)[0]))
    builder.add_node("call_model", model_node)
    builder.add_node("tools", tool_node)
    builder.add_node("approval", approval)
    builder.add_node("report_guard", report_guard)
    builder.add_node("finish", finalize)
    builder.add_edge(START, "prepare_context")
    builder.add_edge("prepare_context", "call_model")
    builder.add_conditional_edges("call_model", after_model)
    builder.add_conditional_edges("tools", after_tools)
    builder.add_edge("approval", "prepare_context")
    builder.add_edge("report_guard", "prepare_context")
    builder.add_edge("finish", END)
    # 继承父图 Saver，并为每次委派使用独立命名空间，保存各自的恢复现场。
    return builder.compile()


def prepare_delegations(state: dict, context) -> dict:
    context.control.check()
    plan = state["plan"]
    completed = {step["id"] for step in plan["steps"] if step["status"] == "completed"}
    ready = [step for step in plan["steps"] if step["status"] == "pending" and set(step.get("depends_on", [])) <= completed]
    if not ready:
        return {"delegations": [], "plan": {**plan, "status": "needs_input" if any(
            step["status"] == "pending" for step in plan["steps"]) else "completed"}}
    ready = ready[:min(plan.get("max_parallel", 1), get_settings().plan_max_parallel)]
    if any(not set(step["snapshot"]["tools"]) <= READ_ONLY_TOOLS for step in ready):
        ready = ready[:1]
    steps = [dict(step) for step in plan["steps"]]
    delegations = []
    for step in steps:
        if step["id"] not in {item["id"] for item in ready}:
            continue
        step["status"] = "running"
        delegations.append({"step_id": step["id"], "agent_id": step["agent_id"], "skill_id": step["skill_id"],
            "snapshot": step["snapshot"], "packet": make_handoff(plan, step), "allow_approval": plan["allow_approval"]})
        active = delegation_context(delegations[-1], context)
        emit_context_event("plan.step.started", active, step_id=step["id"], step_index=steps.index(step) + 1,
                           step_count=len(steps), status="running")
    active_ids = [item["step_id"] for item in delegations]
    return {"plan": {**plan, "steps": steps, "active_steps": active_ids,
                     "current_step": next(index + 1 for index, item in enumerate(steps) if item["id"] in active_ids)},
            "delegations": delegations}


def collect_delegations(state: dict, context) -> dict:
    context.control.check()
    plan, messages = state["plan"], []
    steps = [dict(step) for step in plan["steps"]]
    blocked = False
    needs_input = False
    for step in steps:
        if step["id"] not in plan["active_steps"]:
            continue
        result = SubagentResult.model_validate(state["child_results"][step["id"]])
        step.update(status=result.status, result=result.output.model_dump(mode="json"))
        blocked |= result.status == "blocked"
        needs_input |= result.status == "needs_input"
        emit_context_event(f"plan.step.{result.status}", context, step_id=step["id"], agent_id=str(result.agent_id),
                           skill_id=result.skill_id, status=result.status)
        messages.append(AIMessage(content=f"子任务 {step['id']}：{result.status}。{result.output.reply[:500]}",
                                  response_metadata={"generated_by": "subagent_handoff"}))
    auto = blocked and not needs_input and plan.get("replan_count", 0) < min(plan.get("max_replans", 0), get_settings().plan_max_replans)
    stopped = (blocked or needs_input) and not auto
    if stopped:
        steps = [{**step, "status": "skipped"} if step["status"] == "pending" else step for step in steps]
    return {"plan": {**plan, "steps": steps, "active_steps": [], "status": "needs_input" if stopped else "running"},
            "messages": messages, "auto_replan_needed": auto}


def execution_scopes(state):
    yield state
    for task in state.tasks:
        if hasattr(task.state, "values") and hasattr(task.state, "tasks"):
            yield from execution_scopes(task.state)


def harvest_completed(plan, results):
    if not plan:
        return plan
    steps = [dict(step) for step in plan["steps"]]
    for step in steps:
        handoff = results.get(step["id"])
        if handoff and handoff.get("agent_id") == step.get("agent_id") and handoff["status"] == "completed":
            step.update(status="completed", result=handoff["output"])
    return {**plan, "steps": steps}


def approval_values(state):
    scopes = list(execution_scopes(state))
    for scope in reversed(scopes):
        if scope.values.get("approval") or any(getattr(item, "name", None) == "request_quote_approval"
                                              for item in scope.values.get("messages", [])):
            return scope.values
    return state.values
