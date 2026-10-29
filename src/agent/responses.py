from src.domain.planning import public_plan


def chat_result(result: dict, run_id: str, thread_id: str) -> dict:
    """后台结果只保存公开响应字段，不序列化整段图状态和消息历史。"""
    return {
        "run_id": run_id, "thread_id": thread_id, "reply": result["reply"],
        "vehicle": result.get("vehicle"), "vehicles": result.get("vehicles", []),
        "has_more": result.get("has_more", False), "quote_draft": result.get("quote_draft"),
        "status": result.get("status", "completed"), "approval": result.get("approval"),
        "quote_id": result.get("quote_id"), "skill_id": result.get("skill_id"),
        "skill_version": result.get("skill_version"), "report_sources": result.get("report_sources", []),
        "report_analysis": result.get("report_analysis"),
        "memory_proposals": result.get("memory_proposals", []),
        "web_sources": result.get("web_sources", []),
        "web_search_status": result.get("web_search_status"),
        "plan": public_plan(result.get("plan"), approval=result.get("approval")),
    }
