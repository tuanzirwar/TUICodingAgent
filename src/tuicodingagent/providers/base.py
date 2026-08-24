"""Provider 统一接口、共享 HTTP 生命周期和安全错误转换。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

import httpx

from ..errors import (
    AuthenticationError,
    NetworkError,
    ProtocolError,
    ProviderError,
    RateLimitError,
)
from ..models import ChatMessage, ProviderConfig, ProviderEvent, SystemPrompt, ToolDefinition


@runtime_checkable
class ChatProvider(Protocol):
    config: ProviderConfig

    def stream(
        self,
        system_prompt: SystemPrompt | str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]: ...

    async def close(self) -> None: ...


class BaseHTTPProvider:
    """复用 HTTP 客户端，并只关闭由自身创建的客户端。"""

    def __init__(self, config: ProviderConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20))

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def check_response(self, response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        await response.aread()
        detail = _safe_response_detail(response, self.config.api_key)
        prefix = f"{self.config.name} 请求失败"
        if response.status_code in {401, 403}:
            raise AuthenticationError(f"{prefix}：认证失败{detail}")
        if response.status_code == 429:
            raise RateLimitError(f"{prefix}：请求过于频繁{detail}")
        raise ProviderError(f"{prefix}：HTTP {response.status_code}{detail}")


def normalize_http_error(error: Exception, config: ProviderConfig) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, httpx.TimeoutException):
        return NetworkError(f"{config.name} 请求超时，请稍后重试")
    if isinstance(error, httpx.RequestError):
        return NetworkError(f"{config.name} 网络连接失败：{type(error).__name__}")
    return ProtocolError(f"{config.name} 返回了无法解析的数据")


def _safe_response_detail(response: httpx.Response, secret: str) -> str:
    """提取短错误摘要；密钥与长响应体绝不进入界面。"""

    text = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error", payload)
            if isinstance(error, dict):
                text = str(error.get("message", ""))
            elif isinstance(error, str):
                text = error
    except ValueError:
        text = ""
    text = text.replace(secret, "***").replace("\r", " ").replace("\n", " ")[:200].strip()
    return f"（{text}）" if text else ""
