import json
import re
from urllib.parse import quote

from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import ValidationError

from src.domain.web_search import WebSource


def collect_web_sources(messages: list) -> tuple[list[dict], str | None, str | None]:
    """只收集本轮真实工具结果，避免复用上轮来源或模型虚构的链接。"""
    sources, attempted, failed, error = {}, False, False, None
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            break
        if not isinstance(message, ToolMessage) or message.name != "search_web":
            continue
        attempted = True
        if message.status == "error":
            failed = True
            error = error or str(message.content)[:500]
            continue
        try:
            data = json.loads(message.content)
            if not isinstance(data, dict) or data.get("provider") != "tavily" or not isinstance(data.get("sources"), list):
                raise ValueError("Invalid search result")
            for item in data["sources"][:5]:
                source = WebSource.model_validate(item)
                if source.url not in sources and len(sources) < 10:
                    sources[source.url] = source.model_dump(mode="json")
        except (ValueError, TypeError, ValidationError):
            failed = True
            error = error or "搜索结果格式异常，请重试。"
    status = ("partial" if sources else "failed") if failed else ("completed" if sources else "empty")
    return list(sources.values()), status if attempted else None, error


def render_web_reply(reply: str, sources: list[dict], status: str | None, error: str | None) -> str:
    if status is None:
        return reply
    if status == "failed":
        return "本轮联网搜索未成功，未取得可用网页来源。" + (error or "请稍后重试。")
    if status == "empty":
        return "本轮联网搜索没有取得可用网页来源，请补充车型、年款、地区或调整搜索词。"
    links = {item["citation"]: quote(item["url"], safe=":/?#[]@!$&'*+,;=%~_-") for item in sources}
    reply = re.sub(r"\[(W-[0-9a-f]{12})\]", lambda match:
                   f"[{match[1]}]({links[match[1]]})" if match[1] in links else "[来源未核实]", reply)
    lines = []
    for item in sources:
        title = re.sub(r"([\\\[\]`*_<>])", r"\\\1", item["title"].replace("\n", " "))
        lines.append(f"- [{title}]({links[item['citation']]})")
    # 正文也保留真实来源，旧会话仅加载消息时仍然能回溯网页。
    reply += "\n\n联网来源：\n" + "\n".join(lines)
    if status == "partial":
        reply += "\n\n部分查询失败，以上只列出成功取得的来源。"
    return reply + "\n\n网页信息可能过时，公开挂牌价仅供参考，不代表真实成交价或门店报价。"
