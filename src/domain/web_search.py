from datetime import datetime
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, HttpUrl, field_validator


class WebSearchError(Exception):
    """可向用户展示的搜索失败原因，不包含供应商原始响应或凭据。"""


WebSearchStatus = Literal["completed", "empty", "partial", "failed"]


class WebSource(BaseModel):
    citation: str = Field(pattern=r"^W-[0-9a-f]{12}$")
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    snippet: str = Field(max_length=1600)
    query: str = Field(min_length=1, max_length=400)
    retrieved_at: datetime
    published_date: str | None = Field(default=None, max_length=100)
    source_type: Literal["web_reference"] = "web_reference"

    @field_validator("url")
    @classmethod
    def public_http_url(cls, value: str) -> str:
        value = str(HttpUrl(value))
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        if (parts.scheme not in {"https", "http"} or not host or parts.username or parts.password
                or any(char.isspace() or ord(char) < 32 for char in value)
                or "\\" in value or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError("来源必须是公开网页链接。")
        try:
            address = ip_address(host)
        except ValueError:
            if "." not in host:
                raise ValueError("来源必须是公开域名。")
        else:
            if not address.is_global:
                raise ValueError("不能使用本地地址作为联网来源。")
        return value
