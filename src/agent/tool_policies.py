from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from src.agent.context import AgentContext
from src.agent.middleware import ToolHandler
from src.agent.report_analysis import log_analysis_event, render_report_analysis, validate_report_analysis
from src.agent.tracing import logger as trace_logger
from src.domain.report_analysis import ReportAnalysisError


def vehicle_tool_policy(state: dict, config: RunnableConfig, call_next: ToolHandler, context: AgentContext) -> dict:
    calls, analysis = state["messages"][-1].tool_calls, None
    if any(call["name"] == "finalize_vehicle_report" for call in calls):
        try:
            if len(calls) != 1:
                raise ReportAnalysisError("结构化解读必须单独提交，不能与查询工具并行。")
            analysis = validate_report_analysis(calls[0]["args"], state["messages"])
        except ReportAnalysisError as exc:
            log_analysis_event(trace_logger, "report.analysis.rejected", context)
            return {"messages": [ToolMessage(content=str(exc), name=call["name"], tool_call_id=call["id"],
                                              status="error") for call in calls], "report_analysis": None}
    if any(call["name"] == "request_quote_approval" for call in calls) and (len(calls) != 1 or state.get("approval")):
        return {"messages": [ToolMessage(
            content="审批必须单独提交，一轮只能处理一次审批；请等待用户的新指令。",
            name=call["name"], tool_call_id=call["id"], status="error") for call in calls]}

    result = call_next(state, config)
    if analysis is not None and result["messages"][-1].status != "error":
        output = result["messages"][-1]
        result = {"messages": [ToolMessage(content=analysis.model_dump_json(), name=output.name,
                                            tool_call_id=output.tool_call_id, id=output.id),
                                AIMessage(content=render_report_analysis(analysis),
                                          response_metadata={"generated_by": "verified_report_renderer"})],
                  "report_analysis": analysis.model_dump(mode="json")}
        log_analysis_event(trace_logger, "report.analysis.validated", context)
    return result
