from __future__ import annotations

from pathlib import Path

from tuicodingagent.mcp_client import HttpServerConfig, StdioServerConfig, load_mcp_servers


def test_mcp_config_merges_by_server_and_expands_only_credentials(tmp_path: Path, capsys) -> None:
    user = tmp_path / "user.yaml"
    project = tmp_path / "project.yaml"
    user.write_text(
        """
mcp_servers:
  shared:
    type: stdio
    command: old-command
    args: ["${DO_NOT_EXPAND}"]
  local:
    type: stdio
    command: python
    env:
      TOKEN: "Bearer ${TOKEN}"
""",
        encoding="utf-8",
    )
    project.write_text(
        """
mcp_servers:
  shared:
    type: http
    url: http://localhost/mcp
    headers:
      Authorization: "Bearer ${MISSING_TOKEN}"
""",
        encoding="utf-8",
    )
    servers = load_mcp_servers(
        tmp_path,
        (user, project),
        {"TOKEN": "secret-value", "DO_NOT_EXPAND": "changed"},
    )
    assert len(servers) == 2
    shared = next(item for item in servers if item.name == "shared")
    local = next(item for item in servers if item.name == "local")
    assert isinstance(shared, HttpServerConfig)
    assert shared.headers == {"Authorization": "Bearer "}
    assert isinstance(local, StdioServerConfig)
    assert local.env == {"TOKEN": "Bearer secret-value"}
    assert "secret-value" not in capsys.readouterr().err


def test_mcp_config_skips_invalid_file_and_servers(tmp_path: Path, capsys) -> None:
    invalid = tmp_path / "invalid.yaml"
    valid = tmp_path / "valid.yaml"
    invalid.write_text("mcp_servers: [secret-value", encoding="utf-8")
    valid.write_text(
        """
mcp_servers:
  no_type: {command: python}
  no_command: {type: stdio}
  no_url: {type: http}
  bad_args: {type: stdio, command: python, args: nope}
  good: {type: stdio, command: python, args: [server.py]}
""",
        encoding="utf-8",
    )
    servers = load_mcp_servers(tmp_path, (invalid, valid), {})
    assert [item.name for item in servers] == ["good"]
    warning = capsys.readouterr().err
    assert "已跳过" in warning
    assert "Traceback" not in warning
    assert "secret-value" not in warning


def test_mcp_config_repr_hides_expanded_credentials(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "mcp_servers:\n  web:\n    type: http\n    url: https://example.test/mcp\n"
        "    headers:\n      Authorization: '${TOKEN}'\n",
        encoding="utf-8",
    )
    server = load_mcp_servers(tmp_path, (config, tmp_path / "missing"), {"TOKEN": "hidden"})[0]
    assert "hidden" not in repr(server)
