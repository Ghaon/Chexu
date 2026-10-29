from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import Field

from src.services.web_search import search


@tool
def search_web(query: Annotated[str, Field(min_length=3, max_length=400)], config: RunnableConfig,
               time_range: Literal["day", "week", "month", "year"] | None = None) -> dict:
    """通过 Tavily 搜索公开网页，返回标题、摘要、来源链接和引用编号。用于外部车型资料、政策与公开行情参考；不查询门店库存、真实成交价或私人车况。仅传必要的公开搜索词，不传客户姓名、电话、VIN 或内部经营数据。"""
    return search(query, time_range=time_range, timeout_ms=config["configurable"]["search_timeout_ms"])
