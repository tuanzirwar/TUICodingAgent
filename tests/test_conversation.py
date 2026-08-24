from tuicodingagent.conversation import Conversation
from tuicodingagent.models import ChatMessage, ToolCall, ToolResult


def test_conversation_callbacks_cover_all_mutations() -> None:
    appended: list[ChatMessage] = []
    replaced: list[list[ChatMessage]] = []
    conversation = Conversation(on_append=appended.append, on_replace=replaced.append)
    call = ToolCall("c1", "read_file", {"path": "a.py"})
    result = ToolResult("c1", "read_file", True, output="ok")

    conversation.add_user("问题")
    conversation.add_assistant("回复")
    conversation.add_assistant_with_tool_calls("读取", (call,))
    conversation.add_tool_results((result,))
    conversation.replace_messages((ChatMessage("assistant", "摘要"),))

    assert len(appended) == 4
    assert appended[2].tool_calls == (call,)
    assert appended[3].tool_results == (result,)
    assert replaced == [[ChatMessage("assistant", "摘要")]]
    assert conversation.messages == (ChatMessage("assistant", "摘要"),)


def test_conversation_without_callbacks_is_backward_compatible() -> None:
    conversation = Conversation()
    conversation.add_user("问题")
    conversation.add_assistant("回复")
    assert [message.role for message in conversation.messages] == ["user", "assistant"]
