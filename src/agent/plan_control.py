from uuid import UUID, uuid5

from langchain_core.messages import HumanMessage

from src.agent.config import get_settings
from src.agent.planning import approval_authorized, generate_plan
from src.agent.tracing import emit_context_event
from src.services import plan_actions


def command_gate(state, context):
    command_id = plan_actions.pending(context.thread_id)
    if command_id and str(command_id) in state.get("applied_commands", []):
        plan_actions.finish(command_id, "applied", task_id=context.task_id)
        command_id = plan_actions.pending(context.thread_id)
    return {"active_command_id": str(command_id) if command_id else None}


def after_gate(state):
    if state.get("active_command_id"):
        return "apply_action"
    if state.get("auto_replan_needed"):
        return "auto_replan"
    plan = state["plan"]
    return "prepare_agents" if plan["status"] == "running" and any(step["status"] == "pending" for step in plan["steps"]) else "summarize_plan"


def apply_action(state, config, context, model):
    command = plan_actions.read(UUID(state["active_command_id"]))
    plan, command_id = state["plan"], UUID(command["command_id"])
    if command["status"] in {"applied", "rejected"}:
        return {"active_command_id": None}
    if str(command_id) in state.get("applied_commands", []):
        return {}
    error = None
    if (command["thread_id"] != str(context.thread_id) or command["employee_id"] != str(context.employee_id)
            or command["turn_id"] != str(context.turn_id)):
        error = "ownership_changed"
    elif plan["status"] == "cancelled":
        error = "plan_cancelled"
    elif command["expected_revision"] != plan.get("revision", 1):
        error = "revision_conflict"
    elif plan.get("revision", 1) >= get_settings().plan_max_revisions:
        error = "revision_limit"
    elif command["action"] == "continue" and plan["status"] != "needs_input":
        error = "not_waiting_for_input"
    if plan_actions.is_approval_locked(context.thread_id):
        error = "approval_locked"
    if error:
        plan_actions.finish(command_id, "rejected", error_code=error)
        emit_context_event("plan.action.rejected", context, command_id=str(command_id), error_code=error)
        return {"active_command_id": None}
    context.control.check()
    message = command["payload"]["message"]
    revision = plan.get("revision", 1) + 1
    updated = {**plan, "engine": 2, "revision": revision, "active_steps": [], "current_step": None, "status": "running",
               "objective": plan.get("objective", next(item.content for item in reversed(state["messages"]) if isinstance(item, HumanMessage))),
               "updates": [*plan.get("updates", []), message], "allow_approval": approval_authorized(message, plan["allow_approval"])}
    updated["adjustments"] = [*plan.get("adjustments", []), f"用户{command['action']}：{message[:300]}"]
    output = {}
    if command["action"] == "continue":
        steps = [dict(step) for step in plan["steps"]]
        for index, step in enumerate(steps):
            step.setdefault("depends_on", [steps[index - 1]["id"]] if index else [])
        mapping = {step["id"]: f"r{revision}_step_{index + 1}" for index, step in enumerate(steps) if step["status"] != "completed"}
        for step in steps:
            if step["status"] == "completed":
                continue
            step.update(id=mapping[step["id"]], status="pending", result=None)
            step["agent_id"] = str(uuid5(context.turn_id, step["id"]))
            step["depends_on"] = [mapping.get(item, item) for item in step.get("depends_on", [])]
        updated["steps"] = steps
    else:
        steps = [dict(step) for step in plan["steps"]]
        if command["action"] == "revise":
            start = command["payload"].get("restart_from")
            if start and start not in {step["id"] for step in steps}:
                plan_actions.finish(command_id, "rejected", error_code="unknown_restart_step")
                return {"active_command_id": None}
            position = next((index for index, step in enumerate(steps) if step["id"] == start), 0)
            steps = [step for index, step in enumerate(steps) if index < position and step["status"] == "completed"]
        updated["steps"] = steps
        output = generate_plan(state, config, context, model, previous=updated)
        updated = output["plan"]
    emit_context_event("plan.action.applied", context, command_id=str(command_id), revision=revision, status="running")
    return {**output, "messages": [HumanMessage(content=message), *output.get("messages", [])],
            "plan": updated, "auto_replan_needed": False,
            "applied_commands": [*state.get("applied_commands", []), str(command_id)], "execution_status": None}


def acknowledge_action(state, context):
    if state.get("active_command_id") and state["active_command_id"] in state.get("applied_commands", []):
        plan_actions.finish(UUID(state["active_command_id"]), "applied", task_id=context.task_id)
    return {"active_command_id": None}


def auto_replan(state, config, context, model):
    plan = state["plan"]
    if plan.get("revision", 1) >= get_settings().plan_max_revisions:
        return {"auto_replan_needed": False, "plan": {**plan, "status": "needs_input"}}
    previous = {**plan, "revision": plan.get("revision", 1) + 1, "replan_count": plan.get("replan_count", 0) + 1}
    reason = "；".join((step.get("result") or {}).get("reply", "步骤受阻")[:300]
                      for step in plan["steps"] if step["status"] == "blocked")
    previous["adjustments"] = [*plan.get("adjustments", []), f"自动调整剩余步骤：{reason[:1000]}"]
    result = generate_plan(state, config, context, model, previous=previous)
    return {**result, "auto_replan_needed": False}
