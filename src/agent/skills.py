from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import Lock
import tomllib

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agent.config import PROJECT_ROOT
from src.agent.tools.registry import AGENT_TOOLS


class SkillError(Exception):
    pass


TOOL_ROLES = {
    "get_vehicle": frozenset({"sales", "manager"}),
    "search_vehicles": frozenset({"sales", "manager"}),
    "create_quote_draft": frozenset({"sales", "manager"}),
    "request_quote_approval": frozenset({"sales", "manager"}),
    "list_vehicle_reports": frozenset({"sales", "manager"}),
    "read_vehicle_report": frozenset({"sales", "manager"}),
    "finalize_vehicle_report": frozenset({"sales", "manager"}),
    "propose_user_memory": frozenset({"sales", "manager"}),
    "search_web": frozenset({"sales", "manager"}),
}


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=500)
    tools: tuple[str, ...]
    max_model_calls: int = Field(default=4, ge=1, le=20)
    max_tool_calls: int = Field(default=6, ge=1, le=50)


class SkillManifest(SkillMetadata):
    enabled: bool = True


class SkillSnapshot(SkillMetadata):
    instructions: str
    digest: str


@dataclass(frozen=True)
class SkillDefinition:
    manifest: SkillManifest
    instruction_file: Path


def skill_digest(instructions: str, tools: tuple[str, ...]) -> str:
    return sha256(json.dumps({"instructions": instructions, "tools": tools},
                              ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.skills: dict[str, SkillDefinition] = {}
        self._instruction_cache: dict[str, str] = {}
        self._instruction_lock = Lock()
        try:
            for path in sorted(self.root.glob("*/skill.toml")):
                folder = path.parent.resolve()
                instruction_file = folder / "SKILL.md"
                self._checked_file(path)
                self._checked_file(instruction_file)
                if path.stat().st_size > 8192 or instruction_file.stat().st_size > 65536:
                    raise SkillError("技能配置或说明文件过大。")
                manifest = SkillManifest.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
                if manifest.id != folder.name or manifest.id in self.skills:
                    raise SkillError("技能编号必须与目录一致，且不能重复。")
                known_tools = TOOL_ROLES.keys() & {tool.name for tool in AGENT_TOOLS}
                if len(set(manifest.tools)) != len(manifest.tools) or not set(manifest.tools) <= known_tools:
                    raise SkillError("技能引用了未知或重复的工具。")
                # 启动仅加载元数据和正文位置，正文在技能首次被选中时读取。
                self.skills[manifest.id] = SkillDefinition(manifest, instruction_file)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise SkillError("技能配置无法加载，请检查服务端配置。") from exc
        if "inventory_match" not in self.skills:
            raise SkillError("缺少默认库存匹配技能。")

    def _checked_file(self, path: Path) -> Path:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise SkillError("技能文件必须是项目技能目录内的普通文件。")
        return resolved

    def get_metadata(self, skill_id: str, role: str) -> SkillMetadata:
        entry = self.skills.get(skill_id)
        if entry is None or not entry.manifest.enabled:
            raise SkillError("指定技能不存在或已停用。")
        manifest = entry.manifest
        tools = tuple(name for name in manifest.tools if role in TOOL_ROLES[name])
        return SkillMetadata(**(manifest.model_dump(exclude={"enabled"}) | {"tools": tools}))

    def list_metadata(self, role: str) -> list[SkillMetadata]:
        return [self.get_metadata(skill_id, role) for skill_id, entry in self.skills.items()
                if entry.manifest.enabled]

    def _read_instructions(self, skill_id: str) -> str:
        try:
            # 延迟读取时重新校验路径，并限制实际读取量。
            path = self._checked_file(self.skills[skill_id].instruction_file)
            with path.open("rb") as handle:
                content = handle.read(65537)
            if len(content) > 65536:
                raise SkillError("技能说明文件过大。")
            instructions = content.decode("utf-8").strip()
            if not instructions:
                raise SkillError("技能说明不能为空。")
            return instructions
        except (OSError, UnicodeError) as exc:
            raise SkillError("技能正文无法加载，请检查服务端技能文件。") from exc

    def _load_instructions(self, skill_id: str) -> str:
        # 只缓存与角色无关的正文；权限仍在每次获取元数据时检查。
        with self._instruction_lock:
            if skill_id not in self._instruction_cache:
                self._instruction_cache[skill_id] = self._read_instructions(skill_id)
            return self._instruction_cache[skill_id]

    def get(self, skill_id: str, role: str) -> SkillSnapshot:
        metadata = self.get_metadata(skill_id, role)
        instructions = self._load_instructions(skill_id)
        return SkillSnapshot(**metadata.model_dump(), instructions=instructions,
                             digest=skill_digest(instructions, metadata.tools))

    def select(self, message: str, role: str, explicit: str | None = None,
               previous: str | None = None) -> SkillSnapshot:
        if explicit is not None:
            return self.get(explicit, role)
        # 禁止推断的提醒不是报告解读意图，例如“比较售价，不要推断未知车况”。
        intent = re.sub(r"(?:不要|不得|不能|不应|无需|不必)[^，。；！？\n]{0,16}(?:推断|猜测|假设)[^，。；！？\n]{0,24}", "", message)
        if re.search(r"记住|记下来|保存.{0,8}(?:偏好|预算|用途)|记录.{0,8}(?:偏好|预算|用途)|长期记忆|我的偏好", message):
            chosen = "user_memory"
        elif (not re.search(r"(?:不要|不用|无需|别).{0,4}(?:联网|上网|搜索)", message)
              and re.search(r"联网|上网|网上|网页|外部行情|市场行情|搜索.{0,16}(?:资料|新闻|政策|行情|价格|车型)", message)):
            chosen = "web_research"
        elif re.search(r"检测报告|检测单|维保|维修记录|车况|事故|泡水|补漆", intent):
            chosen = "vehicle_report"
        elif re.search(r"报价|优惠|折扣|审批|审核", message):
            chosen = "quote_assist"
        elif re.search(r"对比|比较|区别|更适合|选哪", message):
            chosen = "vehicle_compare"
        elif re.search(r"找|筛选|库存|预算|有哪些|里程", message):
            chosen = "inventory_match"
        else:
            chosen = previous or "inventory_match"
        return self.get(chosen, role)

    def restore(self, data: dict, role: str) -> SkillSnapshot:
        try:
            old = SkillSnapshot.model_validate(data)
        except ValidationError as exc:
            raise SkillError("运行的技能快照无效，无法恢复。") from exc
        if skill_digest(old.instructions, old.tools) != old.digest:
            raise SkillError("技能快照校验失败，无法恢复。")
        # 恢复已有正文快照，只需读取当前元数据检查权限，不能换成新正文。
        current = self.get_metadata(old.id, role)
        # 保留原说明和版本，只允许当前仍然授权的原工具。
        tools = tuple(name for name in old.tools if name in current.tools)
        if tools != old.tools:
            raise SkillError("技能工具权限已变化，请重新开启任务。")
        return old.model_copy(update={"max_model_calls": min(old.max_model_calls, current.max_model_calls),
                                      "max_tool_calls": min(old.max_tool_calls, current.max_tool_calls)})


@lru_cache
def get_skill_registry() -> SkillRegistry:
    return SkillRegistry(PROJECT_ROOT / "skills")
