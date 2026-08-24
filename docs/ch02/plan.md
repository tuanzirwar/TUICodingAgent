# TUICodingAgent 多协议 LLM 终端对话客户端 Plan

> 状态：技术设计逐段审批中。已批准部分方可作为后续设计依据。

## 架构概览

本章采用 Python 异步架构，以 Textual 承载全屏终端界面，以 HTTPX 建立异步 HTTP/SSE 连接，以 PyYAML 读取配置。项目按“配置与启动、协议适配、会话运行时、界面”四层划分，依赖方向保持单向。

1. **配置与启动层**：负责读取项目根目录的 YAML 文件、完成全部 provider 配置校验、选择单一或多个 provider 的启动路径，并把经过验证的活动配置交给应用。启动期错误在进入全屏 TUI 前转为简洁中文提示。
2. **协议适配层**：定义统一的异步流式 Provider 接口和协议无关的消息/文本增量模型。OpenAI 与 Anthropic 适配器分别构造各自的请求、解析 SSE 事件、过滤 thinking 增量，并把鉴权、限流、网络和协议错误归一化。上层不接触厂商 SDK 类型。
3. **会话运行时层**：维护仅存在于当前进程内的完整消息历史，组合系统提示词与历史，驱动一次异步流式请求；只有成功完成的助手正文才进入历史。它向界面发布开始、文本增量、完成和失败事件，但不承担具体渲染。
4. **终端界面层**：Textual App 负责 provider 选择页和主对话页。主页面由启动横幅、就绪行、可滚动对话区、多行输入框及状态栏组成。耗时计时器和流式消费运行在 Textual Worker 中，避免阻塞事件循环；流式阶段显示纯文本，完成后将对应消息替换为 Markdown 渲染。

整体调用链为：

```text
CLI 启动
  → 加载并验证 YAML
  → 单配置直接选中 / 多配置打开选择页
  → 构造对应 Provider 与 ConversationRuntime
  → 用户提交消息
  → Runtime 携带系统提示词和完整历史请求 Provider.stream()
  → Provider 解析 SSE 并产出正文增量
  → TUI 实时追加纯文本、更新计时
  → 完成后定型为 Markdown；失败则显示错误并恢复输入
```

该分层让 F1–F2 归属配置与启动层，F3–F5 归属协议适配层，F4、F6、F11 归属会话运行时层，F7–F12 归属终端界面层；N1–N7 分别由异步边界、统一事件模型、集中配置校验、错误脱敏和 Textual 终端生命周期共同保证。

## 核心数据结构与接口

### `ProviderConfig`

使用 `@dataclass(frozen=True, slots=True)` 表示一份已经校验通过的 provider 配置：

```python
@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    protocol: Literal["openai", "anthropic"]
    model: str
    base_url: str | None
    api_key: str = field(repr=False)
    thinking: bool = False
```

`name`、`protocol`、`model`、`api_key` 必填且不得为空；`base_url` 为空时由协议适配器使用默认地址。`api_key` 禁止出现在对象 repr 和异常文本中。

### `AppConfig`

```python
@dataclass(frozen=True, slots=True)
class AppConfig:
    providers: tuple[ProviderConfig, ...]
```

`providers` 至少包含一项并保持 YAML 中的声明顺序。配置加载完成后整个对象不可变，避免活动会话中途切换或修改 provider。

### `ChatMessage`

```python
@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str
```

系统提示词不进入普通历史，而是作为独立参数交给 Provider；历史只保存成功提交的用户消息与完整生成的助手正文。

### `RuntimeEvent`

```python
class RuntimeEventType(StrEnum):
    STARTED = "started"
    TEXT_DELTA = "text_delta"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    type: RuntimeEventType
    text: str = ""
    elapsed: float | None = None
```

运行时以统一事件向 TUI 汇报生命周期。`TEXT_DELTA` 只携带可展示正文；`COMPLETED` 携带完整正文与总耗时；`FAILED` 携带已经脱敏、适合展示的中文错误与耗时。

### `ChatProvider`

```python
class ChatProvider(Protocol):
    config: ProviderConfig

    async def stream(
        self,
        system_prompt: str,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...
```

统一接口只产出正文文本增量。协议特有的 SSE 事件、thinking 内容和响应元数据均在适配器内部消费。`close()` 释放复用的异步 HTTP 客户端。

### `ConfigLoader`

```python
class ConfigLoader:
    @staticmethod
    def load(path: Path) -> AppConfig: ...
```

加载器负责文件存在性、YAML 顶层结构、providers 非空、字段类型、必填值和协议枚举校验。所有失败统一抛出不含密钥的 `ConfigError`，CLI 只打印错误摘要并返回非零退出码。

### `ProviderFactory`

```python
def create_provider(config: ProviderConfig) -> ChatProvider: ...
```

工厂根据 `protocol` 创建 `OpenAIProvider` 或 `AnthropicProvider`。未知协议在配置阶段已被拒绝，工厂仍保留防御性错误。

### `ConversationRuntime`

```python
class ConversationRuntime:
    def __init__(self, provider: ChatProvider, system_prompt: str) -> None: ...

    @property
    def history(self) -> tuple[ChatMessage, ...]: ...

    async def chat(self, user_text: str) -> AsyncIterator[RuntimeEvent]: ...

    async def close(self) -> None: ...
```

`chat()` 拒绝空文本和并发调用。开始时暂存用户消息，调用 Provider 并逐个转发正文增量；流正常结束后原子追加用户消息与助手消息。若请求失败，则不污染历史，产出 `FAILED` 并恢复空闲状态，使下一轮可继续。

### 错误类型

```python
class TUICodingAgentError(Exception): ...
class ConfigError(TUICodingAgentError): ...
class ProviderError(TUICodingAgentError): ...
class AuthenticationError(ProviderError): ...
class RateLimitError(ProviderError): ...
class NetworkError(ProviderError): ...
class ProtocolError(ProviderError): ...
```

协议适配器将 HTTP 状态、超时、连接错误和畸形 SSE 归一化为以上类型。错误的用户可见字符串只包含 provider 名、错误类别和安全摘要，不包含请求头、完整响应体或 API 密钥。

### TUI 会话状态

```python
class SessionState(StrEnum):
    IDLE = "idle"
    STREAMING = "streaming"
```

只有 `IDLE` 状态接受提交；进入 `STREAMING` 后禁用输入提交但保留对话区滚动。Worker 完成或失败都必须在 `finally` 路径恢复 `IDLE`。

## 模块设计

### 配置与 CLI

**职责：** 解析固定的 `config.yaml`，在启动 TUI 前完成全部校验，提供清晰的启动错误和进程退出码。

**对外接口：** `ConfigLoader.load()`、`main()`。

**依赖：** PyYAML、配置模型、TUI 应用工厂。CLI 不读取环境变量或 provider 相关命令行覆盖，确保符合本章边界。

### SSE 解析器

**职责：** 将 HTTP 字节/文本行解析为完整 SSE 事件；支持注释、空行派发、连续 `data:` 行拼接和 `[DONE]` 终止标记。

**对外接口：** `iter_sse(response) -> AsyncIterator[SSEEvent]`。

**依赖：** HTTPX 流式响应。解析器不理解 OpenAI 或 Anthropic 业务字段。

### OpenAI 协议适配器

**职责：** 向默认 `/v1/chat/completions` 地址或配置的完整 `base_url` 发起流式请求；把 system prompt 和历史编码为 OpenAI messages；`thinking=true` 时传递 `reasoning_effort="high"`；解析 `choices[].delta.content`，忽略 `reasoning_content` 等思考字段。

**认证：** `Authorization: Bearer <api_key>`。

**依赖：** Provider 基类、SSE 解析器、HTTPX。

### Anthropic 协议适配器

**职责：** 向默认 `/v1/messages` 地址或配置的完整 `base_url` 发起流式请求；system prompt 使用顶层 `system`，历史使用 messages；`thinking=true` 时发送 `thinking={type: enabled, budget_tokens: 4096}` 并设置足够的 `max_tokens`；只产出 `content_block_delta.delta.type == "text_delta"` 的文本，丢弃 `thinking_delta`。

**认证：** `x-api-key` 与固定 `anthropic-version` 请求头。

**依赖：** Provider 基类、SSE 解析器、HTTPX。

### 错误归一化

**职责：** 根据 HTTP 401/403、429、其他非成功状态、连接错误、超时和畸形事件生成安全的中文 `ProviderError` 子类。响应正文只提取已知短消息字段并限制长度，任何与配置密钥相同的片段都会替换为 `***`。

**对外接口：** `raise_for_provider_status(response, config)`、`safe_error_message(error, config)`。

### 会话运行时

**职责：** 串行化每轮请求，维护易于测试的内存历史，并把 Provider 文本流转换为协议无关事件。运行时用 `time.monotonic()` 计时，防止系统时间调整影响耗时。

**对外接口：** `ConversationRuntime.chat()`、`history`、`close()`。

### Provider 选择界面

**职责：** 当 provider 数量大于一时显示方向键可导航的列表，每项展示名称和模型；Enter 确认并切换到主界面。单 provider 时完全跳过该屏幕。

**对外接口：** `ProviderSelectScreen` 返回被选中的 `ProviderConfig`。

### 主对话界面

**职责：** 组合 Banner、就绪行、滚动对话区、`PromptInput` 和 Footer；启动 Worker 消费 `ConversationRuntime.chat()`；增量期间更新 `Static` 纯文本，完成后替换为 Textual `Markdown`；每秒刷新进行中耗时；失败时插入错误样式消息并恢复输入。

**输入约定：** `PromptInput` 基于 Textual `TextArea`。Enter 发布提交消息，Alt+Enter 插入换行。流式期间组件只读且提交处理器检查 `SessionState`，形成界面与逻辑双保险。

**退出约定：** 空闲或流式期间 Ctrl+C 都调用应用退出；输入内容去除首尾空白后等于 `/exit` 时退出。应用卸载时关闭 HTTP 客户端并让 Textual 恢复终端模式。

## 模块交互

### 启动流程

1. `__main__` 调用 CLI `main()`，固定读取当前工作目录下的 `config.yaml`。
2. `ConfigLoader` 一次性加载并校验所有配置；失败则向 stderr 输出单行中文错误，返回退出码 2。
3. CLI 构造 `TUICodingAgentApp`。一个 provider 时直接进入聊天视图；多个时先 push `ProviderSelectScreen`。
4. 选择完成后通过 `ProviderFactory` 创建适配器和 `ConversationRuntime`，挂载聊天视图。

### 单轮对话流程

1. 输入组件发布非空消息；聊天视图处理 `/exit`，否则将状态切为 `STREAMING`、清空并锁定输入。
2. 对话区立即加入用户消息与空的助手流式消息，开始显示动画计时。
3. Worker 迭代 `runtime.chat(text)`；收到 `TEXT_DELTA` 时追加纯文本并滚动到底部。
4. 独立的 Textual interval 每秒读取单调时钟，更新 `Imagining… (Ns)`；它不依赖网络是否已返回首个增量。
5. 收到 `COMPLETED` 时，用完整正文替换为 Markdown 消息并显示总耗时。
6. 收到 `FAILED` 时，移除空助手占位、添加错误消息和耗时。无论何种结果，都在 `finally` 中恢复 `IDLE` 和输入可用状态。

### Provider 流程

1. 适配器把独立 system prompt 和不可变历史转换为协议请求体，显式设置 `stream=true`。
2. HTTPX 使用 `AsyncClient.stream()` 保持连接并逐行交给 SSE 解析器。
3. 适配器校验事件 JSON 与协议字段，只 yield 正文增量；thinking 事件直接跳过。
4. 正常结束后返回；任何异常先归一化并脱敏，再交给运行时生成 `FAILED`。

## 文件组织

```text
TUICodingAgent/
├── pyproject.toml
├── config.example.yaml
├── src/tuicodingagent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── errors.py
│   ├── models.py
│   ├── runtime.py
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── factory.py
│   │   ├── sse.py
│   │   ├── openai.py
│   │   └── anthropic.py
│   └── tui/
│       ├── __init__.py
│       ├── app.py
│       ├── screens.py
│       ├── widgets.py
│       └── app.tcss
└── tests/
    ├── test_config.py
    ├── test_sse.py
    ├── test_providers.py
    ├── test_runtime.py
    ├── test_tui.py
    └── fixtures/
        ├── openai_stream.txt
        └── anthropic_stream.txt
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| Python 版本 | Python 3.11+ | 原生支持 `StrEnum`、TaskGroup 等现代异步能力，同时覆盖常见环境 |
| TUI 框架 | Textual | 原生异步 Worker、Screen、Markdown、TextArea、响应式 CSS 与测试 Pilot 能覆盖完整界面需求 |
| HTTP 客户端 | HTTPX AsyncClient | 支持异步流式响应、连接复用和 MockTransport 测试，不依赖厂商 SDK |
| YAML | PyYAML SafeLoader | 配置结构简单；`safe_load` 避免任意对象构造 |
| 协议实现 | 直接 HTTP + SSE | 精确控制兼容端点、thinking 过滤和错误归一化，上层接口稳定 |
| `base_url` 语义 | 完整请求 URL | 原始需求称其为“请求地址”；避免对兼容服务擅自拼接路径 |
| 历史提交 | 请求成功后成对提交 | 失败轮不产生孤立 user 消息，下一轮上下文保持合法交替 |
| 测试网络 | HTTPX MockTransport/伪流 | 自动化测试完全离线、可确定验证请求体和增量解析；真实 API 留给端到端验收 |
| 样式 | 独立 TCSS | 布局与 Python 行为分离，便于窄屏适配和后续章节扩展 |

## Spec 覆盖与自检

- F1–F2：配置模型、加载器、CLI 与选择 Screen 完整覆盖。
- F3–F5：统一 Provider、两个适配器、SSE 解析和 thinking 过滤完整覆盖。
- F6：`ConversationRuntime` 内存历史与成功后成对提交完整覆盖。
- F7–F10：Textual 主界面、定制输入、Markdown 定型及两种退出路径完整覆盖。
- F11–F12：错误归一化、恢复路径和单调时钟计时完整覆盖。
- N1–N7：异步 Worker、协议统一、启动校验、密钥脱敏、响应式 TCSS 与应用生命周期分别承接。
- 模块依赖为 CLI/TUI → Runtime → Provider → SSE/HTTP，不形成循环。
- 本章没有引入工具调用、持久化、额外 slash 命令、自动重试或其他越界能力。
