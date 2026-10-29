import json
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class CaseExecutionError(Exception):
    pass


class CaseEnvironment:
    def __init__(self, client, owner_id, headers):
        self.client, self.owner_id, self.headers = client, owner_id, headers
        self.artifacts = {"responses": [], "queried_vehicles": {}}

    def call(self, method, path, *, role="sales", **kwargs):
        response = self.client.request(method, path, headers=self.headers[role], **kwargs)
        if response.status_code >= 400:
            if response.status_code < 500:
                detail = response.json().get("detail")
                message = detail if isinstance(detail, str) else detail.get("message") if isinstance(detail, dict) else None
                self.artifacts.setdefault("http_errors", []).append({"status": response.status_code,
                    "message": message[:500] if isinstance(message, str) else "请求校验失败"})
            # 不把供应商原始异常、连接串或登录 token 写入评估报告。
            raise CaseExecutionError(f"http_{response.status_code}")
        return response.json() if response.content else None

    def chat(self, message, skill_id=None, thread_id=None):
        from src.agent.vehicle_agent import build_vehicle_agent
        from src.database.checkpoints import open_checkpointer
        body = {"message": message, "thread_id": thread_id or str(uuid4())}
        if skill_id:
            body["skill_id"] = skill_id
        result = self.call("POST", "/api/chat", json=body)
        self.artifacts["responses"].append(result)
        with open_checkpointer() as saver:
            state = build_vehicle_agent(saver).get_state({"configurable": {"thread_id": body["thread_id"]}})
        messages = state.values.get("messages", [])
        current = max((index for index, item in enumerate(messages) if isinstance(item, HumanMessage)), default=0)
        for item in messages[current:]:
            if not isinstance(item, ToolMessage) or item.status == "error":
                continue
            try:
                data = json.loads(item.content)
            except (ValueError, TypeError):
                continue
            if item.name == "get_vehicle" and isinstance(data, dict) and data.get("vehicle_id"):
                self.artifacts["queried_vehicles"][data["vehicle_id"]] = data
            elif item.name == "search_vehicles" and isinstance(data, dict):
                for vehicle in data.get("vehicles", []):
                    self.artifacts["queried_vehicles"][vehicle["vehicle_id"]] = vehicle
        return result

    def ledger(self):
        from sqlalchemy import func, select
        from src.database.connection import SessionFactory
        from src.database.models import ApprovalRecord, QuoteRecord
        with SessionFactory() as session:
            quotes = session.scalars(select(QuoteRecord).where(QuoteRecord.created_by_employee_id == self.owner_id)).all()
            approvals = session.scalar(select(func.count()).select_from(ApprovalRecord).where(
                ApprovalRecord.requested_by_employee_id == self.owner_id))
            return {"quotes": len(quotes), "approvals": approvals, "quote_amounts": sorted(item.quoted_price_cny for item in quotes)}

    def run(self, case):
        from src.evaluation.catalog import EVAL_ROOT
        thread = None
        if case.scenario == "memory_confirmed":
            self.call("POST", "/api/memories", json={"category": "budget", "value": 100000, "confirm": True})
        elif case.scenario == "report":
            content = (EVAL_ROOT / "fixtures" / "report.txt").read_bytes()
            self.artifacts["report_text"] = content.decode("utf-8")
            self.artifacts["report_metadata"] = self.call("POST", "/api/vehicles/EVAL-001/reports",
                files={"file": ("evaluation-report.txt", content, "text/plain")})
        elif case.scenario == "long_context":
            thread = self.seed_history()
        result = self.chat(case.message, case.skill_id, thread)
        if case.scenario == "approval":
            self.artifacts["before_review"] = self.ledger()
            if result.get("status") == "awaiting_approval":
                approval_id = result["approval"]["approval_id"]
                self.artifacts["review"] = self.call("POST", f"/api/approvals/{approval_id}/review", role="manager",
                    json={"decision": "approve", "note": "隔离评估数据：经理人工审核动作"})
        elif case.scenario == "memory_pending":
            self.artifacts["confirmed_memories"] = self.call("GET", "/api/memories?status=confirmed")
            self.chat("我有没有已确认的预算？只按已确认的长期记忆回答；没有就明确说明没有，不要记录或猜测。", "user_memory")
        elif case.scenario == "long_context":
            self.artifacts["context"] = self.call("GET", f"/api/conversations/{result['thread_id']}/context")
        self.artifacts["ledger"] = self.ledger()
        return self.artifacts

    def seed_history(self):
        from src.agent.vehicle_agent import build_vehicle_agent
        from src.database.checkpoints import open_checkpointer
        thread = self.call("POST", "/api/conversations", json={"title": "评估：合成的长对话历史"})["thread_id"]
        messages = [HumanMessage(content="预算十万元，日常通勤。"), AIMessage(content="", tool_calls=[{
            "name": "search_vehicles", "args": {"max_price_cny": 100000}, "id": "seed-search"}]),
            ToolMessage(content=json.dumps({"vehicles": [{"vehicle_id": "EVAL-001", "price_cny": 80000},
                {"vehicle_id": "EVAL-002", "price_cny": 90000}], "has_more": False}),
                name="search_vehicles", tool_call_id="seed-search"), AIMessage(content="以上为当时的两辆候选车。")]
        for index in range(24):
            messages.extend([HumanMessage(content=f"历史讨论{index}：" + "补充说明。" * 75),
                             AIMessage(content="这些是历史讨论，具体资料需要重新查询。" * 30)])
        with open_checkpointer(thread) as saver:
            build_vehicle_agent(saver).update_state({"configurable": {"thread_id": thread}},
                {"messages": messages, "plan": None}, as_node="finish")
        return thread
