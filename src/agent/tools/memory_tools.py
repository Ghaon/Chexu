import re
from typing import Annotated
from uuid import UUID

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field, ValidationError

from src.domain.memory import MemoryCategory, MemoryInput, MemoryToolError, MemoryValue
from src.services import memories


@tool
def propose_user_memory(category: MemoryCategory, value: MemoryValue,
                        source_quote: Annotated[str, Field(min_length=1, max_length=500)],
                        messages: Annotated[list, InjectedState("messages")], config: RunnableConfig) -> dict:
    """用户明确要求记住时，提出预算（整数元）、用途或偏好的待确认记忆；不能确认或修改已确认记忆。source_quote 必须逐字引用本轮用户原话。"""
    latest = next((message for message in reversed(messages) if isinstance(message, HumanMessage)), None)
    content = latest.content if latest is not None and isinstance(latest.content, str) else ""
    if (source_quote not in content or not re.search(r"记住|记下来|记录|保存", content)
            or re.search(r"(?:不要|别|不必|不用|无需|不需要).{0,12}(?:记住|记下来|记录|保存)", content)):
        raise MemoryToolError("只有用户明确要求记录，且引用本轮用户原话，才能提出待确认记忆。")
    identity = config["configurable"]
    try:
        body = MemoryInput(category=category, value=value)
    except ValidationError as exc:
        raise MemoryToolError("内容与记忆类别不匹配，请检查预算单位、用途文本和偏好数组。") from exc
    try:
        candidate = memories.propose(UUID(identity["employee_id"]), UUID(identity["thread_id"]),
            body, source_quote, identity.get("tool_timeout_ms"))
    except (memories.MemoryConflict, memories.MemoryNotFound) as exc:
        raise MemoryToolError(str(exc)) from exc
    return candidate.model_dump(mode="json")
