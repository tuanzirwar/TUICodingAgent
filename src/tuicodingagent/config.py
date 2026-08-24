"""仅从 YAML 文件加载应用配置。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .models import AppConfig, ProviderConfig

_ALLOWED_FIELDS = {
    "name",
    "protocol",
    "model",
    "base_url",
    "api_key",
    "thinking",
    "context_window",
    "wire_api",
}
_REQUIRED_FIELDS = {"name", "protocol", "model", "api_key"}
_PROTOCOLS = {"openai", "anthropic"}


class ConfigLoader:
    """执行严格、可读且不会泄露密钥的配置校验。"""

    @staticmethod
    def load(path: Path) -> AppConfig:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"配置文件不存在：{path}") from exc
        except OSError as exc:
            raise ConfigError(f"无法读取配置文件：{path}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件 YAML 格式错误：{_yaml_summary(exc)}") from exc

        if not isinstance(raw, dict):
            raise ConfigError("配置顶层必须是映射，并包含 providers 列表")
        providers_raw = raw.get("providers")
        if not isinstance(providers_raw, list) or not providers_raw:
            raise ConfigError("providers 必须是非空列表")

        unknown_top = set(raw) - {"providers", "enable_subagent_background", "features"}
        if unknown_top:
            raise ConfigError(f"配置顶层包含未知字段：{', '.join(sorted(unknown_top))}")
        background = raw.get("enable_subagent_background", True)
        if not isinstance(background, bool):
            raise ConfigError("enable_subagent_background 必须是布尔值")
        features = raw.get("features", {})
        if not isinstance(features, dict) or any(
            not isinstance(key, str) or not isinstance(value, bool)
            for key, value in features.items()
        ):
            raise ConfigError("features 必须是字符串到布尔值的映射")
        providers = tuple(
            ConfigLoader._provider(item, index) for index, item in enumerate(providers_raw, 1)
        )
        return AppConfig(
            providers=providers,
            enable_subagent_background=background,
            features={key.casefold(): value for key, value in features.items()},
        )

    @staticmethod
    def _provider(raw: Any, index: int) -> ProviderConfig:
        label = f"providers[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{label} 必须是映射")
        unknown = set(raw) - _ALLOWED_FIELDS
        if unknown:
            raise ConfigError(f"{label} 包含未知字段：{', '.join(sorted(map(str, unknown)))}")
        missing = _REQUIRED_FIELDS - set(raw)
        if missing:
            raise ConfigError(f"{label} 缺少必要字段：{', '.join(sorted(missing))}")

        for field_name in ("name", "protocol", "model", "api_key"):
            value = raw[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{label}.{field_name} 必须是非空字符串")
        protocol = raw["protocol"].strip().lower()
        if protocol not in _PROTOCOLS:
            raise ConfigError(f"{label}.protocol 仅支持 openai 或 anthropic")

        base_url = raw.get("base_url")
        if base_url is not None and (not isinstance(base_url, str) or not base_url.strip()):
            raise ConfigError(f"{label}.base_url 必须是非空字符串或省略")
        thinking = raw.get("thinking", False)
        if not isinstance(thinking, bool):
            raise ConfigError(f"{label}.thinking 必须是布尔值")
        context_window = raw.get("context_window")
        if context_window is not None and (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            raise ConfigError(f"{label}.context_window 必须是正整数或省略")
        wire_api = raw.get("wire_api")
        if wire_api is not None and (not isinstance(wire_api, str) or not wire_api.strip()):
            raise ConfigError(f"{label}.wire_api 必须是非空字符串或省略")
        normalized_wire = wire_api.strip().lower() if isinstance(wire_api, str) else None
        allowed_wires = {"chat_completions", "responses"} if protocol == "openai" else {"messages"}
        if normalized_wire is not None and normalized_wire not in allowed_wires:
            choices = "、".join(sorted(allowed_wires))
            raise ConfigError(f"{label}.wire_api 在 {protocol} 协议下仅支持：{choices}")

        return ProviderConfig(
            name=raw["name"].strip(),
            protocol=protocol,  # type: ignore[arg-type]
            model=raw["model"].strip(),
            base_url=base_url.strip() if base_url else None,
            api_key=raw["api_key"].strip(),
            thinking=thinking,
            context_window=context_window,
            wire_api=normalized_wire,  # type: ignore[arg-type]
        )


def _yaml_summary(error: yaml.YAMLError) -> str:
    """只保留解析器第一行，避免把含密钥的 YAML 片段回显。"""

    text = str(error).splitlines()[0].strip()
    return text[:160] or "无法解析"
