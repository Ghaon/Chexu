import httpx
from langchain_openai import ChatOpenAI

from src.agent.config import Settings, get_settings


def create_model(settings: Settings) -> ChatOpenAI:
    # 仅控制模型客户端的代理，不修改进程环境或关闭 TLS 证书校验。
    return ChatOpenAI(
        model=settings.model_name,
        base_url=settings.model_base_url,
        api_key=settings.model_api_key.get_secret_value(),
        timeout=settings.model_timeout_seconds,
        max_retries=0,
        http_socket_options=(),
        http_client=httpx.Client(trust_env=settings.model_use_env_proxy, timeout=settings.model_timeout_seconds),
        http_async_client=httpx.AsyncClient(trust_env=settings.model_use_env_proxy, timeout=settings.model_timeout_seconds),
    )


settings = get_settings()
model = create_model(settings)

# 可用工具由当前技能在每次调用时提供，不在全局客户端绑定所有工具。
tool_model = model
