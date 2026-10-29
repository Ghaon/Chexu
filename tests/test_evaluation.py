from copy import deepcopy
import unittest

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from src.evaluation.catalog import load_cases, load_vehicles
from src.evaluation.grading import grade
from src.evaluation.metrics import EvaluationBudgetExceeded, ModelBudget, summarize


class EvaluationTests(unittest.TestCase):
    def case(self, scenario):
        return next(case for case in load_cases() if case.scenario == scenario)

    def test_inventory_scoring_rejects_missing_extra_and_incorrect_vehicle_facts(self):
        vehicles = load_vehicles()[:2]
        artifacts = {"responses": [{"vehicles": vehicles, "reply": "EVAL-001 EVAL-002"}],
                     "tool_names": ["search_vehicles"]}
        self.assertTrue(all(item["passed"] for item in grade(self.case("inventory"), artifacts, load_vehicles())))
        for invalid in (vehicles[:1], load_vehicles(), [dict(vehicles[0], price_cny=1), vehicles[1]]):
            with self.subTest(invalid=invalid):
                wrong = deepcopy(artifacts)
                wrong["responses"][0]["vehicles"] = invalid
                self.assertFalse(all(item["passed"] for item in grade(self.case("inventory"), wrong, load_vehicles())))

    def test_approval_scoring_checks_database_before_human_review(self):
        artifacts = {"responses": [{"status": "awaiting_approval", "approval": {"status": "pending"}}],
            "before_review": {"quotes": 1, "approvals": 1},
            "review": {"execution_status": "completed", "approval": {"status": "completed", "quote_id": "fixture"}},
            "ledger": {"quotes": 1, "quote_amounts": [84000]}}
        checks = grade(self.case("approval"), artifacts, load_vehicles())
        self.assertFalse(next(item["passed"] for item in checks if item["name"] == "经理操作前没有确认报价"))

    def test_literal_citation_does_not_prove_statement_semantics(self):
        reference = {"report_id": "fixture", "sha256": "fixturehash", "quote": "发动机舱未见明显渗漏。"}
        artifacts = {"responses": [{"report_analysis": {"vehicle_id": "EVAL-001",
            "facts": [{"statement": "车辆无事故", "evidence": [reference]}]}}],
            "report_metadata": {"report_id": "fixture", "sha256": "fixturehash"},
            "report_text": reference["quote"], "ledger": {"quotes": 0, "approvals": 0}}
        checks = grade(self.case("report"), artifacts, load_vehicles())
        self.assertTrue(next(item["passed"] for item in checks if item["name"] == "全部逐字引用来自目标原文"))
        self.assertFalse(next(item["passed"] for item in checks if item["name"] == "没有把缺失检测推断为无事故或无泡水"))

    def test_budget_blocks_request_and_missing_usage_keeps_cost_unknown(self):
        budget = ModelBudget(1)
        model = FakeMessagesListChatModel(responses=[AIMessage(content="fixture")]).with_config(callbacks=[budget])
        model.invoke("hello")
        with self.assertRaises(EvaluationBudgetExceeded):
            model.invoke("second request")
        self.assertEqual(budget.snapshot()["model_calls"], 1)
        self.assertEqual(budget.snapshot()["usage_reported_calls"], 0)
        report = summarize([{"status": "passed", "duration_seconds": 1, "metrics": budget.snapshot()},
                            {"status": "not_run_budget", "duration_seconds": 0, "metrics": {}}], 1, 2)
        self.assertEqual(report["success_rate_executed"], 1)
        self.assertEqual(report["completion_rate_selected"], 0.5)
        self.assertFalse(report["usage_complete"])
        self.assertIsNone(report["estimated_cost_usd"])

    def test_empty_plan_does_not_pass_agent_isolation_check(self):
        checks = grade(self.case("plan"), {"responses": [{"plan": {"steps": []}}]}, load_vehicles())
        self.assertFalse(next(item["passed"] for item in checks if item["name"] == "步骤具有独立子Agent编号"))


if __name__ == "__main__":
    unittest.main()
