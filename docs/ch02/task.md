# TUICodingAgent 多协议 LLM 终端对话客户端 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 新建 | `pyproject.toml` | 包元数据、运行依赖、测试与 lint 配置、CLI 入口 |
| 新建 | `config.example.yaml` | 双协议安全配置示例 |
| 新建 | `src/tuicodingagent/{__init__,__main__,cli,config,errors,models,runtime}.py` | 基础模型、配置、运行时与入口 |
| 新建 | `src/tuicodingagent/providers/*.py` | 统一 Provider、SSE、工厂与双协议适配器 |
| 新建 | `src/tuicodingagent/tui/*.py`、`app.tcss` | Textual 界面、Screen、Widget 与样式 |
| 新建 | `tests/test_*.py`、`tests/fixtures/*` | 配置、协议、运行时和界面测试 |
| 新建 | `README.md` | 安装、配置与运行说明 |

## T1：建立 Python 包骨架

**文件：** `pyproject.toml`、`src/tuicodingagent/__init__.py`、`__main__.py`
**依赖：** 无
**步骤：**
1. 声明 Python 3.11+、Textual、HTTPX、PyYAML 依赖及 pytest/ruff 开发依赖。
2. 注册 `tuicodingagent` 命令入口并暴露版本号。
3. 让 `python -m tuicodingagent` 委托 CLI `main()`。
**验证：** `python -m compileall src` 无错误；安装后 `python -m tuicodingagent --version` 输出版本。

## T2：定义领域模型与安全错误

**文件：** `src/tuicodingagent/models.py`、`errors.py`
**依赖：** T1
**步骤：**
1. 实现不可变的 Provider/App 配置、聊天消息、运行时事件与会话状态。
2. 定义配置和 Provider 错误层级。
3. 确保 ProviderConfig repr 不包含密钥。
**验证：** 单测断言模型不可变、密钥不在 repr、事件类型正确。

## T3：实现 YAML 配置加载

**文件：** `src/tuicodingagent/config.py`、`config.example.yaml`、`tests/test_config.py`
**依赖：** T2
**步骤：**
1. 用 `yaml.safe_load` 读取固定结构并保留 provider 顺序。
2. 校验文件、YAML、顶层键、列表非空、六个字段的类型与必填值。
3. 把所有失败归一化为不泄露密钥的中文 ConfigError。
4. 提供不含真实密钥的双协议示例。
**验证：** 有效单/多 provider、文件缺失、坏 YAML、缺字段、未知协议和 repr 安全测试通过。

## T4：实现通用 SSE 解析器

**文件：** `src/tuicodingagent/providers/sse.py`、`tests/test_sse.py`
**依赖：** T1
**步骤：**
1. 定义 SSEEvent。
2. 支持注释、event、连续 data 行、空行派发与流结束残余事件。
3. 保持解析器协议无关。
**验证：** 使用异步行夹具验证事件名、拼接 data 和注释忽略。

## T5：实现 Provider 基础能力与错误归一化

**文件：** `providers/base.py`、`tests/test_providers.py`
**依赖：** T2、T4
**步骤：**
1. 定义 ChatProvider Protocol 与共享 HTTP 基类。
2. 实现 HTTP 状态/网络/超时/协议错误映射。
3. 限制错误摘要长度并替换配置密钥。
**验证：** 401、429、500、超时和含密钥响应均映射为正确且安全的错误。

## T6：实现 OpenAI 流式适配器

**文件：** `providers/openai.py`、`tests/fixtures/openai_stream.txt`、`tests/test_providers.py`
**依赖：** T5
**步骤：**
1. 构造 Bearer 认证、system+history、stream 请求体和默认/自定义 URL。
2. thinking 开启时添加 reasoning_effort。
3. 解析正文 delta，忽略 reasoning_content，并识别 `[DONE]`。
**验证：** MockTransport 断言请求体/URL/头并验证多段正文和思考过滤。

## T7：实现 Anthropic 流式适配器

**文件：** `providers/anthropic.py`、`tests/fixtures/anthropic_stream.txt`、`tests/test_providers.py`
**依赖：** T5
**步骤：**
1. 构造 x-api-key、anthropic-version、顶层 system、history 与 stream 请求体。
2. thinking 开启时添加预算配置。
3. 只产出 text_delta，丢弃 thinking_delta。
**验证：** MockTransport 断言请求体/URL/头并验证正文解析和思考过滤。

## T8：实现 Provider 工厂

**文件：** `providers/factory.py`、`providers/__init__.py`、`tests/test_providers.py`
**依赖：** T6、T7
**步骤：** 根据协议创建对应适配器，并允许测试注入 AsyncClient。
**验证：** 两个协议返回正确类型，防御性未知协议路径报错。

## T9：实现会话运行时

**文件：** `src/tuicodingagent/runtime.py`、`tests/test_runtime.py`
**依赖：** T2、T8
**步骤：**
1. 实现 system prompt、只读历史、单轮锁和生命周期事件。
2. 流式转发正文，成功后成对提交历史。
3. 失败时生成安全 FAILED 事件且不污染历史。
4. 用 monotonic 计算总耗时并关闭 Provider。
**验证：** 多轮上下文、增量顺序、失败恢复、并发拒绝和 close 测试通过。

## T10：实现基础 TUI Widget

**文件：** `tui/widgets.py`、`tests/test_tui.py`
**依赖：** T1
**步骤：**
1. 实现猫咪 Banner、用户/助手/错误消息容器。
2. 实现基于 TextArea 的 PromptInput：Enter 提交、Alt+Enter 换行、占位提示。
3. 提交后清空输入。
**验证：** Textual Pilot 验证布局文本、Enter 消息和 Alt+Enter 内容。

## T11：实现 Provider 选择 Screen

**文件：** `tui/screens.py`、`tests/test_tui.py`
**依赖：** T2、T10
**步骤：**
1. 用 OptionList 展示名称与模型。
2. 支持方向键和 Enter，dismiss 所选配置。
**验证：** Pilot 选择第二项并断言返回相应 ProviderConfig。

## T12：实现主聊天应用布局

**文件：** `tui/app.py`、`tui/app.tcss`、`tests/test_tui.py`
**依赖：** T9–T11
**步骤：**
1. 构建 Banner、就绪行、滚动对话区、PromptInput 和状态栏。
2. 单 provider 直接进入，多 provider 先打开选择 Screen。
3. 状态栏显示活动名称与模型，TCSS 支持窄屏。
**验证：** Pilot 对单/多配置截图式 DOM 断言通过。

## T13：接入流式渲染和计时

**文件：** `tui/app.py`、`tui/widgets.py`、`tests/test_tui.py`
**依赖：** T12
**步骤：**
1. Worker 异步消费 RuntimeEvent，期间禁用提交但保留滚动。
2. 首增量前显示 Imagining 动画和秒数，增量到达后实时追加纯文本。
3. 完成后替换为 Markdown 并定型总耗时。
4. 失败时显示区别样式并恢复输入。
**验证：** 可控延迟假 Provider 验证等待、增量、Markdown、耗时和失败恢复。

## T14：实现退出与资源清理

**文件：** `tui/app.py`、`tests/test_tui.py`
**依赖：** T13
**步骤：**
1. 处理 `/exit` 与 Ctrl+C。
2. 应用卸载时关闭 Runtime/HTTP 客户端。
3. 所有 Worker 路径用 finally 恢复状态。
**验证：** Pilot 验证两种退出和 close 调用，交互进程退出码为 0。

## T15：实现 CLI 启动边界

**文件：** `src/tuicodingagent/cli.py`、`tests/test_cli.py`
**依赖：** T3、T12
**步骤：**
1. 固定读取当前目录 `config.yaml` 并启动应用。
2. 增加 `--version`，不增加 provider/model 配置覆盖。
3. 捕获 ConfigError，stderr 输出安全中文摘要并返回 2。
**验证：** CliRunner/子进程验证成功参数、缺配置与坏配置行为。

## T16：文档与全量质量检查

**文件：** `README.md`
**依赖：** T1–T15
**步骤：**
1. 编写中文安装、六字段配置、双协议、自定义 URL、启动与退出说明。
2. 运行 ruff、pytest、compileall，并修复全部问题。
**验证：** `ruff check src tests`、`pytest`、`python -m compileall -q src tests` 全部通过。

## T17：端到端验收

**文件：** `docs/ch02/checklist.md`
**依赖：** T16
**步骤：**
1. 在 tmux 启动 TUICodingAgent。
2. 用可用的真实 Anthropic/OpenAI 兼容 API 完成多轮对话、Markdown、计时和错误恢复验证。
3. 逐项记录 checklist 证据；若无 API 配置，向用户索取。
**验证：** checklist 所有适用项标记通过并记录命令/观察结果。

## 执行顺序

```text
T1 → T2 → T3
 │    └→ T5 → T6 ─┐
 └→ T4 ─────→ T7 ─┴→ T8 → T9
 └→ T10 → T11 → T12 → T13 → T14
T3 + T12 → T15
全部实现 → T16 → T17
```

