from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.agent.config import PROJECT_ROOT
from src.agent.skills import SkillError, SkillRegistry, TOOL_ROLES
from src.agent.tools.registry import AGENT_TOOLS


class SkillRegistryTests(unittest.TestCase):
    def test_startup_and_skill_catalog_do_not_open_instruction_files(self):
        from src.api_view import skills as skill_api
        from src.api_view.schemas import EmployeeResponse

        original_open = Path.open

        def metadata_only(path, *args, **kwargs):
            self.assertNotEqual(path.name, "SKILL.md", "预加载和技能列表不应读取技能正文。")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", autospec=True, side_effect=metadata_only):
            registry = SkillRegistry(PROJECT_ROOT / "skills")
            catalog = registry.list_metadata("sales")
            self.assertEqual(len(catalog), 7)
            self.assertTrue(all("instructions" not in item.model_dump() for item in catalog))
            with patch.object(skill_api, "get_skill_registry", return_value=registry):
                employee = EmployeeResponse(employee_id=uuid4(), username="skill_test", role="sales")
                self.assertEqual(skill_api.list_skills(employee), catalog)

    def test_only_selected_body_is_read_once_and_permissions_are_rechecked(self):
        registry = SkillRegistry(PROJECT_ROOT / "skills")
        with patch.object(registry, "_read_instructions", wraps=registry._read_instructions) as read:
            first = registry.select("比较这两辆车", "sales")
            again = registry.get("vehicle_compare", "sales")
            self.assertEqual(first, again)
            read.assert_called_once_with("vehicle_compare")
            with patch.dict(TOOL_ROLES, {"get_vehicle": frozenset({"manager"})}):
                restricted = registry.get("vehicle_compare", "sales")
            self.assertNotIn("get_vehicle", restricted.tools)
            self.assertEqual(restricted.instructions, first.instructions)
            read.assert_called_once_with("vehicle_compare")
            registry.get("quote_assist", "sales")
            self.assertEqual([call.args[0] for call in read.call_args_list], ["vehicle_compare", "quote_assist"])

    def test_concurrent_first_use_loads_body_once(self):
        registry = SkillRegistry(PROJECT_ROOT / "skills")
        barrier = Barrier(5)

        def load():
            barrier.wait(timeout=5)
            return registry.get("inventory_match", "sales")

        with patch.object(registry, "_read_instructions", wraps=registry._read_instructions) as read, \
             ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(load) for _ in range(5)]
            snapshots = [future.result(timeout=5) for future in futures]
        read.assert_called_once_with("inventory_match")
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))

    def test_resume_uses_snapshot_without_reading_current_body(self):
        original = SkillRegistry(PROJECT_ROOT / "skills").get("quote_assist", "sales")
        registry = SkillRegistry(PROJECT_ROOT / "skills")
        with patch.object(registry, "_read_instructions", side_effect=AssertionError("恢复不应读取新正文")) as read:
            restored = registry.restore(original.model_dump(mode="json"), "sales")
        self.assertEqual(restored, original)
        read.assert_not_called()

    def test_invalid_body_is_checked_on_first_use_and_failure_is_not_cached(self):
        cache = PROJECT_ROOT / ".cache/tmp"
        cache.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=cache) as directory:
            root = Path(directory) / "skills"
            shutil.copytree(PROJECT_ROOT / "skills", root)
            body = root / "vehicle_compare/SKILL.md"
            body.write_text(" \n ", encoding="utf-8")
            registry = SkillRegistry(root)
            self.assertEqual(len(registry.list_metadata("sales")), 7)
            with self.assertRaisesRegex(SkillError, "不能为空"):
                registry.get("vehicle_compare", "sales")
            body.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(SkillError, "无法加载"):
                registry.get("vehicle_compare", "sales")
            body.write_text("有效的技能说明", encoding="utf-8")
            self.assertEqual(registry.get("vehicle_compare", "sales").instructions, "有效的技能说明")

    def test_routing_cases_and_explicit_selection(self):
        registry = SkillRegistry(PROJECT_ROOT / "skills")
        for line in (PROJECT_ROOT / "evals/skill_routing.jsonl").read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            with self.subTest(case=case):
                result = registry.select(case["message"], "sales", case.get("explicit"), case.get("previous"))
                self.assertEqual(result.id, case["expected"])

    def test_snapshot_pins_instructions_but_checks_current_permissions(self):
        cache = PROJECT_ROOT / ".cache/tmp"
        cache.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=cache) as directory:
            root = Path(directory) / "skills"
            shutil.copytree(PROJECT_ROOT / "skills", root)
            old = SkillRegistry(root).get("quote_assist", "sales")
            (root / "quote_assist/SKILL.md").write_text("新版本报价说明", encoding="utf-8")
            manifest = root / "quote_assist/skill.toml"
            manifest.write_text(manifest.read_text(encoding="utf-8").replace('"1.0.0"', '"2.0.0"'), encoding="utf-8")
            restored = SkillRegistry(root).restore(old.model_dump(mode="json"), "sales")
            self.assertEqual(restored.instructions, old.instructions)
            self.assertEqual(restored.version, "1.0.0")
            content = manifest.read_text(encoding="utf-8")
            manifest.write_text(content.replace(', "request_quote_approval"', ''), encoding="utf-8")
            with self.assertRaises(SkillError):
                SkillRegistry(root).restore(old.model_dump(mode="json"), "sales")
            manifest.write_text(content.replace("enabled = true", "enabled = false"), encoding="utf-8")
            with self.assertRaises(SkillError):
                SkillRegistry(root).restore(old.model_dump(mode="json"), "sales")
            manifest.write_text(content.replace('"request_quote_approval"', '"unknown_tool"'),
                                encoding="utf-8")
            with self.assertRaises(SkillError):
                SkillRegistry(root)

    def test_snapshot_tampering_and_unknown_skills_are_rejected(self):
        registry = SkillRegistry(PROJECT_ROOT / "skills")
        snapshot = registry.get("inventory_match", "sales").model_dump(mode="json")
        snapshot["instructions"] = "改变权限"
        with self.assertRaises(SkillError):
            registry.restore(snapshot, "sales")
        with self.assertRaises(SkillError):
            registry.get("unknown", "sales")

    def test_server_context_is_hidden_from_model_tool_schema(self):
        for tool in AGENT_TOOLS:
            schema = tool.tool_call_schema.model_json_schema()
            self.assertNotIn("config", schema.get("properties", {}))
            self.assertNotIn("employee_id", schema.get("properties", {}))


if __name__ == "__main__":
    unittest.main()
