from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tuicodingagent.config import ConfigLoader
from tuicodingagent.errors import ConfigError


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_single_provider_and_hide_secret(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """providers:
  - name: 本地 OpenAI
    protocol: OPENAI
    model: demo-model
    api_key: super-secret
    thinking: true
    context_window: 100000
""",
    )
    config = ConfigLoader.load(path)
    provider = config.providers[0]

    assert provider.name == "本地 OpenAI"
    assert provider.protocol == "openai"
    assert provider.thinking is True
    assert provider.context_window == 100000
    assert config.enable_subagent_background is True
    assert "super-secret" not in repr(provider)
    with pytest.raises(FrozenInstanceError):
        provider.name = "change"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("providers: []", "非空列表"),
        ("providers:\n  - name: x", "缺少必要字段"),
        (
            "providers:\n  - name: x\n    protocol: other\n    model: m\n    api_key: k",
            "仅支持",
        ),
        (
            "providers:\n  - name: x\n    protocol: openai\n"
            "    model: m\n    api_key: k\n    thinking: 'yes'",
            "布尔值",
        ),
        (
            "providers:\n  - name: x\n    protocol: openai\n"
            "    model: m\n    api_key: k\n    context_window: 0",
            "正整数",
        ),
        ("providers: [", "YAML 格式错误"),
    ],
)
def test_invalid_config_is_readable(tmp_path: Path, content: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        ConfigLoader.load(_write(tmp_path / "config.yaml", content))


def test_missing_config_is_readable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="配置文件不存在"):
        ConfigLoader.load(tmp_path / "missing.yaml")


def test_unknown_field_does_not_echo_secret(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """providers:
  - name: demo
    protocol: openai
    model: m
    api_key: do-not-print
    unexpected: value
""",
    )
    with pytest.raises(ConfigError) as caught:
        ConfigLoader.load(path)
    assert "do-not-print" not in str(caught.value)


def test_subagent_background_switch(tmp_path: Path) -> None:
    config = ConfigLoader.load(
        _write(
            tmp_path / "config.yaml",
            """providers:
  - name: demo
    protocol: openai
    model: m
    api_key: k
enable_subagent_background: false
""",
        )
    )
    assert config.enable_subagent_background is False

    path = tmp_path / "invalid.yaml"
    _write(
        path,
        """providers:
  - name: demo
    protocol: openai
    model: m
    api_key: k
enable_subagent_background: nope
""",
    )
    with pytest.raises(ConfigError, match="enable_subagent_background"):
        ConfigLoader.load(path)


def test_openai_responses_wire_api_and_protocol_validation(tmp_path: Path) -> None:
    config = ConfigLoader.load(
        _write(
            tmp_path / "config.yaml",
            """providers:
  - name: responses
    protocol: openai
    wire_api: responses
    model: gpt-5.5
    base_url: https://gateway.example
    api_key: k
""",
        )
    )
    assert config.providers[0].wire_api == "responses"

    invalid = """providers:
  - name: invalid
    protocol: anthropic
    wire_api: responses
    model: claude
    api_key: k
"""
    with pytest.raises(ConfigError, match="wire_api"):
        ConfigLoader.load(_write(tmp_path / "invalid-wire.yaml", invalid))
