# TUICodingAgent 工具系统 Plan

## 架构概览

在第 2 章 Provider 与 Runtime 之间加入协议无关的工具模型和 `ToolRegistry`。Provider 从只产出字符串升级为产出 `ProviderEvent(TEXT_DELTA|TOOL_CALL)`；Runtime 负责固定的“两阶段”流程：第一次请求收集调用、顺序执行并发出工具事件，第二次请求仅收集最终文本且禁止再次执行。TUI 消费工具事件并挂载独立工具行。

## 核心数据结构与接口

```python
@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, object]

@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]
    raw_arguments: str = ""

@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    name: str
    success: bool
    output: str = ""
    error: str = ""
    metadata: dict[str, object] = field(default_factory=dict)
    truncated: bool = False

class Tool(Protocol):
    definition: ToolDefinition
    async def execute(self, arguments: Mapping[str, object]) -> ToolResult: ...

class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool | None: ...
    def definitions(self) -> tuple[ToolDefinition, ...]: ...
    def api_definitions(self, protocol: ProtocolName) -> list[dict]: ...
    async def execute(self, call: ToolCall) -> ToolResult: ...
```

`ChatMessage` 扩展 `tool_calls` 与 `tool_results`，role 扩展为 `tool`。`ProviderEvent` 统一正文与已组装调用；`RuntimeEventType` 增加 `TOOL_STARTED`、`TOOL_FINISHED`、`LIMIT_REACHED`。

Provider 接口变为：

```python
async def stream(
    system_prompt: str,
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolDefinition] = (),
) -> AsyncIterator[ProviderEvent]: ...
```

## 模块设计

- `tools/base.py`：Tool 协议、参数提取、文本截断和结构化结果工厂。
- `tools/files.py`：Read/Write/Edit；同步磁盘操作放进 `asyncio.to_thread`。本章按 Spec 不限制绝对路径和 `..`。
- `tools/shell.py`：`asyncio.create_subprocess_shell`，工作目录为启动 cwd；`wait_for` 超时后 kill；stdout/stderr 各有上限。
- `tools/search.py`：glob 与正则 grep；跳过二进制/不可读文件，结果排序且限制条数。
- `tools/registry.py`：固定注册、协议定义导出、外层超时、未知工具与未捕获异常归一化。
- `providers/openai.py`：发送 `tools=[{type:function,function:...}]`；按 tool index 拼接 id/name/arguments；历史序列化为 assistant tool_calls + 每个 role=tool 结果。
- `providers/anthropic.py`：发送 Anthropic tool schema；拼接 `tool_use`/`input_json_delta`；历史序列化为 assistant tool_use blocks + user tool_result blocks。
- `runtime.py`：首次请求注入定义；执行全部调用并回灌；第二次请求不执行新调用；成功历史原子提交。
- `tui/widgets.py`：`ToolEntry` 展示调用、结果和失败/截断状态。

## 两阶段交互

```text
用户消息
 → Provider.stream(history+user, six_tools)
 → text delta / completed ToolCall(s)
 → Runtime 逐个 TOOL_STARTED → Registry.execute → TOOL_FINISHED
 → history 追加 assistant(tool_calls) + tool(results)
 → Provider.stream(updated_history, six_tools)
 → 最终正文：提交整轮历史并完成
 → 若仍有 ToolCall：发 LIMIT_REACHED，不执行，结束本轮
```

失败的工具结果 `success=false` 仍正常进入第二次请求；只有 Provider 请求失败才保持原有“整轮不提交历史”策略。

## 文件组织

```text
src/tuicodingagent/
├── models.py, runtime.py
├── tools/
│   ├── __init__.py, base.py, registry.py
│   ├── files.py, shell.py, search.py
├── providers/{base,openai,anthropic}.py
└── tui/{app,widgets}.py
tests/
├── test_tools.py
├── test_tool_providers.py
├── test_tool_runtime.py
└── test_tool_tui.py
```

## 技术决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 工具执行接口 | 全异步，磁盘任务 `to_thread` | TUI 不阻塞且实现简单 |
| 命令 | 系统 shell | 符合编码 Agent 预期并保留跨平台能力；权限留到 ch06 |
| 结果协议 | 统一 JSON 文本 + 结构化内存模型 | 双协议一致，模型可稳定理解 |
| 限制 | 读 2000 行/200k 字符，输出 100k 字符，搜索/glob 200 项 | 控制上下文且保留足够诊断信息 |
| 超时 | Registry 30 秒，命令内部 30 秒 | 双层保护；本章按要求不可配置 |
| 第二次工具调用 | 不执行并提示单轮上限 | 严守 ch03 与 ch04 边界 |

## 覆盖自检

F1–F9、N1–N6 均有明确模块与事件承接；双协议历史格式独立封装于适配器，上层无厂商类型；没有权限、沙箱、持久化、并行或 Agent Loop 越界功能。

