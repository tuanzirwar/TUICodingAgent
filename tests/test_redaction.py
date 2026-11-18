"""持久化凭据脱敏与恢复语义，使用人工假凭据。"""

from pathlib import Path

import pytest

from tuicodingagent.eval.harness_redaction import run
from tuicodingagent.models import ChatMessage, ToolCall
from tuicodingagent.redaction import MASK, redact
from tuicodingagent.session import SessionContext, Writer, restore_session, scan_sessions
from tuicodingagent.trace import export_events


@pytest.mark.parametrize(
    "text",
    [
        "API_KEY=FakeCredential1234567890",
        '{"api_key": "FakeCredential1234567890"}',
        'password="FakeCredential1234567890"',
        "Authorization: Bearer FakeCredential1234567890",
        "postgresql://fixture:FakeCredential1234567890@localhost/demo",
        "-----BEGIN PRIVATE KEY-----\nFakeCredential1234567890\n-----END PRIVATE KEY-----",
    ],
)
def test_credential_shapes_are_redacted(text: str) -> None:
    assert "FakeCredential1234567890" not in redact(text)
    assert MASK in redact(text)


def test_task_contacts_and_ordinary_code_are_preserved() -> None:
    text = "timeout=17; retry=2; contact=user@example.invalid"
    assert redact(text) == text
    assert "user@example.invalid" not in redact(text, personal_data=True)
    assert redact({"password": "fake", "timeout": 17}) == {"password": MASK, "timeout": 17}


def test_redacted_saved_arguments_cannot_be_replayed(tmp_path: Path) -> None:
    context = SessionContext.create(tmp_path)
    original = ToolCall(
        "write", "write_file", {"path": "config.txt", "content": "API_KEY=FakeCredential1234567890"}
    )
    with Writer(context, "fixture") as writer:
        writer.append(ChatMessage("user", "建立配置"))
        writer.append(ChatMessage("assistant", tool_calls=(original,)))
    restored = restore_session(scan_sessions(tmp_path)[0])
    call, _ = restored.tool_outcomes[0]
    assert call.parse_error
    assert "FakeCredential1234567890" not in context.conversation_path.read_text(encoding="utf-8")
    assert original.arguments["content"] == "API_KEY=FakeCredential1234567890"


async def test_all_persistence_sinks_hide_fake_credentials(tmp_path: Path) -> None:
    result = await run(tmp_path)
    assert all(row["credentials_passed"] and row["exercised"] for row in result["cases"])
    # 联系方式保留在任务存档/记忆/工件；审计元数据额外遮盖它。
    trace = next(row for row in result["cases"] if row["sink"] == "trace_metadata")
    assert not trace["personal_data_exposed"]


def test_exports_redact_legacy_trace_rows(tmp_path: Path) -> None:
    event = {
        "event_type": "legacy",
        "metadata": {"detail": "API_KEY=FakeCredential1234567890 contact=user@example.invalid"},
    }
    for path in export_events([event], tmp_path):
        content = path.read_text(encoding="utf-8-sig")
        assert "FakeCredential1234567890" not in content
        assert "user@example.invalid" not in content
