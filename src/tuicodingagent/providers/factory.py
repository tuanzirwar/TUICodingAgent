"""Provider 工厂。"""

from __future__ import annotations

import httpx

from ..errors import ConfigError
from ..models import ProviderConfig
from .anthropic import AnthropicProvider
from .base import ChatProvider
from .openai import OpenAIProvider
from .openai_responses import OpenAIResponsesProvider


def create_provider(
    config: ProviderConfig, client: httpx.AsyncClient | None = None
) -> ChatProvider:
    if config.protocol == "openai":
        if config.wire_api == "responses":
            return OpenAIResponsesProvider(config, client)
        return OpenAIProvider(config, client)
    if config.protocol == "anthropic":
        return AnthropicProvider(config, client)
    raise ConfigError(f"不支持的 provider 协议：{config.protocol}")
