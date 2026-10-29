from datetime import datetime, timezone
from hashlib import sha256
from dataclasses import replace
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from uuid import UUID
from langgraph.types import Command

from src.agent.config import get_settings
from src.agent.context import AgentContext, create_context
from src.agent.skills import get_skill_registry
from src.agent.vehicle_agent import build_vehicle_agent
from src.agent.tracing import AgentTrace, emit_context_event
from src.database.checkpoints import open_checkpointer
from src.services.conversations import ConversationAccessError, ensure_conversation
from src.services import approvals, executions, tasks, plan_actions
from src.agent.subagents import approval_values, harvest_completed
from src.domain.tasks import TaskConflict
from src.domain.planning import cancel_plan, public_plan
from src.domain.execution import ExecutionCancelled, ExecutionTimedOut, RunConflict


class ConversationInterruptedError(Exception):
    pass


class ApprovalPendingError(ConversationInterruptedError):
    def __init__(self, approval_id: UUID):
        self.approval_id = approval_id
        super().__init__("该会话正在等待审批，请先处理审批任务或取消申请。")


def pending_approval_id(state) -> UUID | None:
    for task in state.tasks:
        if hasattr(task.state, "tasks"):
            nested = pending_approval_id(task.state)
            if nested:
                return nested
            # 父任务尚未交回结果时，可能仍带着子图上一次暂停的 interrupt。
            if task.state.values.get("approval"):
                continue
        for item in task.interrupts:
            if isinstance(item.value, dict) and "approval_id" in item.value:
                return UUID(item.value["approval_id"])
    return None


def execution_config(thread_id: str, employee_id: UUID, trace: AgentTrace) -> dict:
    return {
        "configurable": {"thread_id": thread_id, "employee_id": str(employee_id), "run_id": trace.run_id},
        "recursion_limit": max(3 * get_settings().max_model_calls + 6,
                               8 * get_settings().plan_max_model_calls + 10 * get_settings().plan_max_revisions + 20),
        "callbacks": [trace],
    }


def present_result(result: dict, trace: AgentTrace) -> dict:
    pauses = result.get("__interrupt__", ())
    if not pauses:
        return result | {"status": "completed", "plan": public_plan(result.get("plan"))}
    task = approvals.get_task(UUID(pauses[0].value["approval_id"]))
    trace.paused_approval_id = str(task.approval_id)
    return {
        "reply": "报价已提交，正在等待经理审核。", "vehicle": None, "vehicles": [],
        "has_more": False, "quote_draft": task.quote_draft.model_dump(mode="json"),
        "status": "awaiting_approval", "approval": task.model_dump(mode="json"), "quote_id": None,
        "plan": public_plan(result.get("plan"), status="awaiting_approval"),
    }


def cancel_graph(graph, config: dict, *, thread_id: UUID, employee_id: UUID) -> dict:
    state = graph.get_state(config, subgraphs=True)
    scope = approval_values(state)
    plan = harvest_completed(state.values.get("plan"), state.values.get("child_results", {}))
    messages = scope.get("messages", [])
    human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    proposal = next((m for m in reversed(messages)
                     if isinstance(m, ToolMessage) and m.name == "request_quote_approval"), None)
    current_round = messages[messages.index(human) + 1:] if human is not None else []
    task = None
    if proposal is not None and proposal in current_round and human is not None:
        key = sha256(f"{human.id}:{proposal.tool_call_id}".encode("utf-8")).hexdigest()
        task = approvals.abort_execution(thread_id, employee_id, key)
    saved = scope.get("approval")
    if scope is state.values and plan:
        saved = next(((step.get("result") or {}).get("approval") for step in reversed(plan["steps"])
                      if (step.get("result") or {}).get("approval")), saved)
    if task is not None:
        saved = task.model_dump(mode="json")
    elif saved:
        audit = approvals.get_task(UUID(saved["approval_id"]))
        if audit.quote_id and not audit.workflow_completed:
            audit = approvals.finish_resume(audit.approval_id, completed=True)
        saved = audit.model_dump(mode="json")
    # 取消可能发生在 AI 发出工具调用之后，补齐未完成的工具应答以保持消息协议完整。
    round_messages = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        round_messages.append(message)
    answered = {m.tool_call_id for m in round_messages if isinstance(m, ToolMessage)}
    missing = [ToolMessage(content="执行已取消，该调用未返回结果。", name=call["name"],
                           tool_call_id=call["id"], status="error")
               for message in reversed(round_messages) if isinstance(message, AIMessage)
               for call in message.tool_calls if call["id"] not in answered]
    quote_id = saved.get("quote_id") if saved else None
    reply = "执行已取消。" + ("此前已保存的报价仍然有效，取消不会撤销该报价。" if quote_id else "")
    result = {"reply": reply, "vehicle": None, "vehicles": [], "has_more": False,
              "quote_draft": saved.get("quote_draft") if saved else None,
              "approval": saved, "quote_id": quote_id, "report_sources": [], "report_analysis": None,
              "web_sources": [], "web_search_status": None,
              "execution_status": "cancelled", "plan": cancel_plan(plan)}
    if scope is not state.values:
        missing = []
    if result.get("plan"):
        plan_actions.reject_pending(thread_id, "plan_cancelled")
    graph.update_state(config, result | {"messages": [*missing, AIMessage(content=reply)]}, as_node="finish")
    return result | {"status": "cancelled"}


def execute(graph, graph_input, config: dict, context: AgentContext, trace: AgentTrace,
            *, actor_id: UUID, parent_id: UUID | None = None, approval_id: UUID | None = None,
            worker_token: UUID | None = None, defer_task_outcome: bool = False) -> dict:
    trace.skill_id, trace.skill_version = context.skill.id, context.skill.version
    if context.task_id:
        tasks.begin_execution(context.task_id, context.run_id, context.employee_id, context.thread_id, context.skill.id,
                              worker_token, lease_seconds=context.control.duration_seconds)
        trace.event_sink = lambda event, data: tasks.record_event(context.task_id, context.run_id, event, data)
    executions.start_run(run_id=context.run_id, turn_id=context.turn_id, thread_id=context.thread_id,
                         employee_id=context.employee_id, actor_id=actor_id, snapshot=context.snapshot(),
                         parent_id=parent_id, approval_id=approval_id)
    try:
        with trace.execution():
            try:
                result = graph.invoke(graph_input, config=config)
                context.control.check()
                result = present_result(result, trace)
                if result["status"] == "awaiting_approval":
                    plan_actions.reject_pending(context.thread_id, "approval_locked")
                pending_id = UUID(trace.paused_approval_id) if trace.paused_approval_id else approval_id
                executions.finish_run(context.run_id, result["status"], approval_id=pending_id)
            except ExecutionCancelled:
                result = cancel_graph(graph, config, thread_id=context.thread_id, employee_id=context.employee_id)
                if result.get("plan"):
                    emit_context_event("plan.cancelled", context, step_count=len(result["plan"]["steps"]), status="cancelled")
                trace.paused_approval_id = None
                trace.emit("run.cancelled")
                executions.finish_run(context.run_id, "cancelled", approval_id=approval_id)
            except Exception as exc:
                status = "timed_out" if isinstance(exc, ExecutionTimedOut) else "failed"
                executions.finish_run(context.run_id, status, error_type=type(exc).__name__, approval_id=approval_id)
                raise
    except Exception as exc:
        if context.task_id and not defer_task_outcome:
            tasks.record_outcome(context.task_id, context.run_id, error=exc)
        raise
    result = result | {"skill_id": context.skill.id, "skill_version": context.skill.version,
                       "plan": public_plan(result.get("plan"))}
    if context.task_id and not defer_task_outcome:
        # 运行结束事件已写入，再提交最终状态，避免事件流提前关闭。
        tasks.record_outcome(context.task_id, context.run_id, result=result)
    return result


def run_chat(message: str, thread_id: str, run_id: str, employee_id: UUID,
             *, role: str = "sales", skill_id: str | None = None, task_id: UUID | None = None,
             worker_token: UUID | None = None) -> dict:
    trace = AgentTrace(run_id, thread_id, str(employee_id))
    config = execution_config(thread_id, employee_id, trace)
    with open_checkpointer(thread_id) as checkpointer:
        state = build_vehicle_agent(checkpointer).get_state(config, subgraphs=True)
        try:
            ensure_conversation(thread_id, employee_id, message, has_checkpoint=bool(state.values))
        except ConversationAccessError:
            trace.emit("run.rejected", error_type="ConversationAccessError")
            raise
        try:
            tasks.ensure_thread_available(UUID(thread_id), task_id)
        except TaskConflict as exc:
            raise ConversationInterruptedError(str(exc)) from exc
        if state.next:
            pending_id = pending_approval_id(state)
            if pending_id is not None:
                raise ApprovalPendingError(pending_id)
            raise ConversationInterruptedError("上次执行尚未结束，请通过运行记录恢复，或取消后再发起新一轮。")
        previous = (state.values.get("context_snapshot") or {}).get("skill", {}).get("id")
        skill = get_skill_registry().select(message, role, explicit=skill_id, previous=previous)
        context = create_context(employee_id, role, UUID(thread_id), UUID(run_id), UUID(run_id), skill,
                                 settings=get_settings(), task_id=task_id)
        graph = build_vehicle_agent(checkpointer, context=context)
        return execute(graph, {
            "messages": [HumanMessage(content=message)], "vehicle": None, "vehicles": [], "has_more": False,
            "quote_draft": None, "reply": "", "approval": None, "quote_id": None, "report_sources": [], "report_analysis": None,
            "execution_status": None, "plan": None, "step_message_start": 0, "memory_proposals": [],
            "web_sources": [], "web_search_status": None,
            "child_results": {"__reset__": True}, "applied_commands": [], "active_command_id": None,
            "auto_replan_needed": False,
            "model_calls_used": 0, "tool_calls_used": 0, "turn_id": run_id, "context_snapshot": context.snapshot(),
        }, config, context, trace, actor_id=employee_id, worker_token=worker_token)


def restored_context(state, run_id: str, employee_id: UUID, role: str, thread_id: str,
                     fallback_turn: UUID) -> tuple[AgentContext, dict | None]:
    turn_id = UUID(state.values["turn_id"]) if state.values.get("turn_id") else fallback_turn
    parent = executions.latest(turn_id)
    snapshot = parent["context_snapshot"] if parent else state.values.get("context_snapshot")
    registry = get_skill_registry()
    skill = registry.restore(snapshot["skill"], role) if snapshot else registry.get("quote_assist", role)
    models, tools, _ = executions.usage(turn_id)
    baseline_model = snapshot.get("baseline_model", 0) if snapshot else state.values.get("model_calls_used", 0)
    baseline_tools = snapshot.get("baseline_tools", 0) if snapshot else state.values.get("tool_calls_used", 0)
    context = create_context(employee_id, role, UUID(thread_id), UUID(run_id), turn_id, skill,
                             used_model=models + baseline_model, used_tools=tools + baseline_tools,
                             baseline_model=baseline_model, baseline_tools=baseline_tools, settings=get_settings(),
                             task_id=UUID(snapshot["task_id"]) if snapshot and snapshot.get("task_id") else None)
    if snapshot:
        context.budget.max_model_calls = min(context.budget.max_model_calls, snapshot.get("max_model_calls", context.budget.max_model_calls))
        context.budget.max_tool_calls = min(context.budget.max_tool_calls, snapshot.get("max_tool_calls", context.budget.max_tool_calls))
        duration = min(context.control.duration_seconds, snapshot.get("max_run_seconds", context.control.duration_seconds))
        context.control.deadline -= context.control.duration_seconds - duration
        context.control.duration_seconds = duration
    return context, parent


def run_plan_action(thread_id: UUID, run_id: UUID, employee_id: UUID, role: str, task_id: UUID,
                    command_id: UUID, worker_token: UUID) -> dict:
    trace = AgentTrace(str(run_id), str(thread_id), str(employee_id))
    config = execution_config(str(thread_id), employee_id, trace)
    with open_checkpointer(str(thread_id)) as saver:
        state = build_vehicle_agent(saver).get_state(config, subgraphs=True)
        ensure_conversation(str(thread_id), employee_id, "", has_checkpoint=bool(state.values))
        if not state.values.get("plan") or state.values["plan"]["status"] == "cancelled" or pending_approval_id(state):
            raise RunConflict("当前计划不可调整，或正在等待审批。")
        context, parent = restored_context(state, str(run_id), employee_id, role, str(thread_id), run_id)
        if context.skill.id != "deal_review":
            raise RunConflict("当前会话已进入另一轮任务，无法调整原计划。")
        context = replace(context, task_id=task_id)
        graph = build_vehicle_agent(saver, context=context)
        if str(command_id) in state.values.get("applied_commands", []):
            return execute(graph, None, config, context, trace, actor_id=employee_id,
                           parent_id=parent["run_id"] if parent else None, worker_token=worker_token)
        plan = harvest_completed(state.values["plan"], state.values.get("child_results", {}))
        graph.update_state(config, {"plan": plan, "active_command_id": str(command_id),
                                    "context_snapshot": context.snapshot()}, as_node="action_start")
        return execute(graph, None, config, context, trace, actor_id=employee_id,
                       parent_id=parent["run_id"] if parent else None, worker_token=worker_token)


def resume_run(source_id: UUID, employee_id: UUID, role: str, run_id: str) -> dict:
    source = executions.get_run(source_id, employee_id)
    if source.employee_id != employee_id or source.thread_id is None:
        raise RunConflict("仅会话发起人可以恢复该执行，审批请通过审批接口处理。")
    thread_id = str(source.thread_id)
    trace = AgentTrace(run_id, thread_id, str(employee_id))
    config = execution_config(thread_id, employee_id, trace)
    with open_checkpointer(thread_id) as saver:
        graph = build_vehicle_agent(saver)
        state = graph.get_state(config, subgraphs=True)
        ensure_conversation(thread_id, employee_id, "", has_checkpoint=bool(state.values))
        latest = executions.latest(source.turn_id)
        if latest is None or latest["run_id"] != source_id or state.values.get("turn_id") != str(source.turn_id):
            raise RunConflict("只能恢复该轮最新的执行记录。")
        if source.status in {"running", "cancel_requested"}:
            # 拿到会话锁说明原工作线程已退出；只有超过截止时间才认定为遗留执行。
            if source.deadline_at > datetime.now(timezone.utc):
                raise RunConflict("该执行尚未超过截止时间，请稍后再恢复。")
            executions.finish_run(source_id, "timed_out", error_type="WorkerInterrupted")
        elif source.status not in {"failed", "timed_out"}:
            raise RunConflict("只有失败或超时的执行可以恢复。")
        if pending_approval_id(state) or state.values.get("approval") or source.approval_id:
            raise RunConflict("该执行涉及人工审批，请通过审批审核或取消接口继续。")
        if not state.next:
            raise RunConflict("该执行没有待恢复节点。")
        if source.cancel_requested:
            result = cancel_graph(graph, config, thread_id=source.thread_id, employee_id=employee_id)
            executions.finish_run(source_id, "cancelled")
            snapshot = state.values.get("context_snapshot") or {}
            if snapshot.get("task_id"):
                tasks.record_outcome(UUID(snapshot["task_id"]), source_id, result=result)
            return result | {"run_id": str(source_id), "thread_id": thread_id,
                             "skill_id": source.skill_id, "skill_version": source.skill_version}
        if executions.usage(source.turn_id)[2] - 1 >= get_settings().max_run_resumes:
            raise RunConflict("本轮恢复次数已达上限，请取消执行后开启新一轮。")
        context, parent = restored_context(state, run_id, employee_id, role, thread_id, source.turn_id)
        graph = build_vehicle_agent(saver, context=context)
        result = execute(graph, None, config, context, trace, actor_id=employee_id, parent_id=source_id)
        return result | {"run_id": run_id, "thread_id": thread_id}


def cancel_run(run_id: UUID, employee_id: UUID):
    source = executions.get_run(run_id, employee_id)
    if source.status in {"running", "cancel_requested", "cancelled"}:
        return executions.request_cancel(run_id, employee_id)
    if source.status not in {"failed", "timed_out"} or source.thread_id is None:
        raise RunConflict("该执行已结束；待审批任务请使用审批取消接口。")
    thread_id = str(source.thread_id)
    with open_checkpointer(thread_id) as saver:
        latest = executions.latest(source.turn_id)
        graph = build_vehicle_agent(saver)
        config = {"configurable": {"thread_id": thread_id, "employee_id": str(source.employee_id)}}
        state = graph.get_state(config, subgraphs=True)
        if latest is None or latest["run_id"] != run_id or state.values.get("turn_id") != str(source.turn_id):
            raise RunConflict("只能取消该轮最新的执行记录。")
        pending_id = pending_approval_id(state)
        if pending_id and approvals.get_task(pending_id).status == "pending":
            raise RunConflict("该执行已暂停等待审批，请使用审批取消接口。")
        result = cancel_graph(graph, config, thread_id=source.thread_id, employee_id=source.employee_id)
        executions.finish_run(run_id, "cancelled")
        snapshot = state.values.get("context_snapshot") or {}
        if snapshot.get("task_id"):
            tasks.record_outcome(UUID(snapshot["task_id"]), run_id, result=result)
    return executions.get_run(run_id, employee_id)


def resume_approval(approval_id: UUID, actor_id: UUID, decision: str, note: str,
                    run_id: str, *, manager: bool):
    task = approvals.get_task(approval_id, actor_id, manager)
    if task.thread_id is None:
        task = approvals.decide_task(approval_id, actor_id, decision, note)
        return task, "completed" if task.workflow_completed else "failed"
    thread_id = str(task.thread_id)
    trace = AgentTrace(run_id, thread_id, str(actor_id))
    trace.emit("approval.review", approval_id=str(approval_id), decision=decision)
    with open_checkpointer(thread_id) as checkpointer:
        task = approvals.get_task(approval_id, actor_id, manager)
        graph = build_vehicle_agent(checkpointer)
        config = execution_config(thread_id, task.requested_by_employee_id, trace)
        state = graph.get_state(config, subgraphs=True)
        pending_id = pending_approval_id(state)
        previous = approval_values(state).get("approval") or {}
        if task.status == "pending" and pending_id != approval_id and decision != "cancel":
            raise approvals.ApprovalConflict("该任务没有可审核的暂停节点，请取消申请后重新提交。")
        context, parent = None, None
        if decision != "cancel" and state.next and (pending_id == approval_id or previous.get("approval_id") == str(approval_id)):
            context, parent = restored_context(state, run_id, task.requested_by_employee_id,
                                               executions.current_role(task.requested_by_employee_id), thread_id, approval_id)
        task = approvals.decide_task(approval_id, actor_id, decision, note)
        if pending_id != approval_id and previous.get("approval_id") != str(approval_id):
            if decision == "cancel" and not state.next:
                task = approvals.finish_resume(approval_id, completed=True)
            return task, "completed" if task.workflow_completed else "failed"
        if not state.next:
            task = approvals.finish_resume(approval_id, completed=True)
            return task, "completed"
        if decision == "cancel":
            result = cancel_graph(graph, config, thread_id=task.thread_id, employee_id=task.requested_by_employee_id)
            task = approvals.finish_resume(approval_id, completed=True)
            turn_id = UUID(state.values["turn_id"]) if state.values.get("turn_id") else approval_id
            parent = executions.latest(turn_id)
            if parent:
                executions.finish_run(parent["run_id"], "cancelled", approval_id=approval_id)
                snapshot = parent["context_snapshot"]
                if snapshot.get("task_id"):
                    tasks.record_outcome(UUID(snapshot["task_id"]), parent["run_id"],
                                         result=result | {"approval": task.model_dump(mode="json")})
            trace.emit("approval.cancelled", approval_id=str(approval_id))
            return task, "completed"
        graph = build_vehicle_agent(checkpointer, context=context)
        approvals.reserve_resume(approval_id)
        try:
            graph_input = Command(resume={"approval_id": str(approval_id)}) if pending_id == approval_id else None
            result = execute(graph, graph_input, config, context, trace, actor_id=actor_id,
                    parent_id=parent["run_id"] if parent else None, approval_id=approval_id, defer_task_outcome=True)
            completed = not graph.get_state(config).next
            task = approvals.finish_resume(approval_id, completed=completed)
            if context.task_id:
                result = result | {"approval": task.model_dump(mode="json"),
                                   "plan": public_plan(result.get("plan"), approval=task.model_dump(mode="json"))}
                tasks.record_outcome(context.task_id, context.run_id, result=result)
            return task, "completed" if completed else "waiting"
        except Exception as exc:
            # 审核或报价可能已经提交；保留事实，后续仅恢复未完成节点。
            task = approvals.finish_resume(approval_id, completed=False, error_type=type(exc).__name__)
            if context.task_id:
                tasks.record_outcome(context.task_id, context.run_id, error=exc)
            return task, "failed"
