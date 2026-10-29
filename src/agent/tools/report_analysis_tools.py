from langchain_core.tools import tool

from src.domain.report_analysis import ReportAnalysisInput, ReportFindingInput


@tool(args_schema=ReportAnalysisInput)
def finalize_vehicle_report(vehicle_id: str, facts: list[ReportFindingInput] | None = None,
                             unknowns: list[ReportFindingInput] | None = None,
                             follow_ups: list[ReportFindingInput] | None = None) -> dict:
    """提交车辆报告的结构化解读，每项附本轮已读报告编号、片段号及逐字摘录。

    facts 为报告记载，unknowns 为未知或缺失信息，follow_ups 为待人工复检事项。
    仅在成功读取原文之后单独调用，不能与其他工具并行。后端校验通过后生成最终回答。
    """
    return ReportAnalysisInput(vehicle_id=vehicle_id, facts=facts or [], unknowns=unknowns or [],
                                follow_ups=follow_ups or []).model_dump(mode="json")
