from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import ValidationError

from src.agent.tracing import emit_context_event
from src.domain.report_analysis import (ReportAnalysisError, ReportAnalysisInput, ReportCoverage,
                                       VehicleReportAnalysis, VerifiedReportFinding, VerifiedReportReference)
from src.domain.reports import ReportEvidence, VehicleReportReadResult


def collect_report_evidence(messages) -> tuple[dict, dict]:
    current_round = []
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        current_round.append(message)
    evidence, reports = {}, {}
    for message in reversed(current_round):
        if not isinstance(message, ToolMessage) or message.name != "read_vehicle_report" or message.status == "error":
            continue
        try:
            data = VehicleReportReadResult.model_validate_json(message.content)
        except (ValidationError, TypeError) as exc:
            raise ReportAnalysisError("报告工具结果无法校验，请重新读取报告。") from exc
        report_key = str(data.report_id)
        previous = reports.get(report_key)
        if previous and (previous["sha256"] != data.sha256 or previous["vehicle_id"] != data.vehicle_id
                         or previous["total_chunks"] != data.total_chunks):
            raise ReportAnalysisError("同一报告的资料标识不一致，请重新开启任务。")
        info = reports.setdefault(report_key, {
            "report_id": data.report_id, "vehicle_id": data.vehicle_id, "filename": data.filename,
            "sha256": data.sha256, "total_chunks": data.total_chunks, "read_chunks": set(), "truncated": False,
        })
        info["truncated"] |= data.truncated
        for chunk in data.chunks:
            if chunk.number > data.total_chunks or chunk.citation != f"[报告 {data.report_id}#片段{chunk.number}]":
                raise ReportAnalysisError("报告片段标识无效，请重新读取报告。")
            item = ReportEvidence(report_id=data.report_id, vehicle_id=data.vehicle_id, filename=data.filename,
                                  sha256=data.sha256, chunk=chunk.number, citation=chunk.citation, text=chunk.text)
            key = (report_key, chunk.number)
            if key in evidence and evidence[key].text != item.text:
                raise ReportAnalysisError("同一报告片段的原文不一致，停止生成解读。")
            evidence[key] = item
            info["read_chunks"].add(chunk.number)
    return evidence, reports


def validate_report_analysis(arguments: dict, messages) -> VehicleReportAnalysis:
    try:
        draft = ReportAnalysisInput.model_validate(arguments)
    except ValidationError as exc:
        raise ReportAnalysisError("解读格式无效：需要车辆编号，以及带 evidence 原文引用的 facts、unknowns 或 follow_ups。") from exc
    evidence, reports = collect_report_evidence(messages)
    if not evidence:
        raise ReportAnalysisError("本轮尚未成功读取报告，必须先调用 read_vehicle_report。")

    def verify(items):
        verified = []
        for item in items:
            references = []
            seen = set()
            for reference in item.evidence:
                source = evidence.get((str(reference.report_id), reference.chunk))
                if source is None:
                    raise ReportAnalysisError("引用必须来自本轮实际读取的报告片段，不能使用未读取或历史轮次的片段。")
                if source.vehicle_id != draft.vehicle_id:
                    raise ReportAnalysisError("引用的报告不属于本次解读车辆，不能混用其他车辆的资料。")
                if reference.quote not in source.text:
                    raise ReportAnalysisError("引用摘录与原文不一致，请逐字摘录本轮返回的片段，不得改写或拼接原文。")
                key = (reference.report_id, reference.chunk, reference.quote)
                if key in seen:
                    continue
                seen.add(key)
                references.append(VerifiedReportReference(**reference.model_dump(), filename=source.filename,
                                                          sha256=source.sha256, citation=source.citation))
            verified.append(VerifiedReportFinding(statement=item.statement, evidence=references))
        return verified

    facts, unknowns, follow_ups = verify(draft.facts), verify(draft.unknowns), verify(draft.follow_ups)
    coverage = []
    for info in reports.values():
        if info["vehicle_id"] != draft.vehicle_id:
            continue
        unread = info["total_chunks"] - len(info["read_chunks"])
        coverage.append(ReportCoverage(
            **{key: info[key] for key in ("report_id", "filename", "sha256", "total_chunks", "truncated")},
            read_chunks=sorted(info["read_chunks"]), unread_chunks=unread,
            extracted_text_complete=unread == 0 and not info["truncated"],
        ))
    limitations = [
        "结果依据本轮提取的报告文字，不代表对当前实车车况的鉴定。",
        "扫描页、图片、印章及图表未完成图像核验；上传关联不等于已核验 VIN 或报告真实性。",
        "系统已核对引用和逐字摘录，陈述与原文的语义关系仍需人工复核。",
    ]
    if any(item.unread_chunks for item in coverage):
        limitations.append("存在尚未读取的文本片段，不能据此声称整份报告没有其他问题。")
    if any(item.truncated for item in coverage):
        limitations.append("报告文字提取达到长度上限，当前结果仅覆盖部分内容。")
    return VehicleReportAnalysis(vehicle_id=draft.vehicle_id, facts=facts, unknowns=unknowns,
                                  follow_ups=follow_ups, coverage=coverage, limitations=limitations)


def render_report_analysis(analysis: VehicleReportAnalysis) -> str:
    lines = [f"车辆 {analysis.vehicle_id} 的报告文字解读："]
    for title, items in (("报告记载的事实", analysis.facts), ("未知或缺失信息", analysis.unknowns),
                         ("待人工复检事项", analysis.follow_ups)):
        lines.extend(["", f"{title}："])
        if not items:
            lines.append("- 本次未列出此类事项，不表示相关项目已确认正常。")
        for item in items:
            citations = " ".join(dict.fromkeys(reference.citation for reference in item.evidence))
            lines.append(f"- {item.statement} {citations}")
    lines.extend(["", "读取范围："])
    for report in analysis.coverage:
        lines.append(f"- {report.filename}：已读取 {len(report.read_chunks)}/{report.total_chunks} 个可用文本片段。")
    lines.extend(["", "说明：", *[f"- {limitation}" for limitation in analysis.limitations]])
    return "\n".join(lines)


def log_analysis_event(logger, event: str, context) -> None:
    emit_context_event(event, context, event_logger=logger)
