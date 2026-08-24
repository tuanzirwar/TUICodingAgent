# TUICodingAgent 第 2 章 Checklist

> 每一项均通过运行代码或观察行为验证；实施完成后在条目下记录实际证据。

## 配置与启动

- [x] C1（AC1）：单 provider 配置直接进入对话；配置文件缺失、YAML 错误、必要字段缺失均显示清晰中文错误且无堆栈。（证据：`test_config.py`、`test_cli.py`、单 provider Pilot 通过）
- [x] C2（AC2）：多 provider 配置显示可用方向键选择的名称+模型列表，确认后状态栏显示所选项。（证据：`test_multi_provider_direction_selection` Textual Pilot 通过；当前主机用 ConPTY 替代 tmux）
- [x] C3（N5）：配置对象 repr、启动错误、请求错误和 TUI 日志均不出现 API 密钥。（证据：repr、未知字段与 HTTP 错误脱敏测试通过）

## 双协议与流式

- [ ] C4（AC3）：Anthropic 与 OpenAI（含自定义完整 URL）通过相同 Provider 接口正常流式收发。（证据：MockTransport 双协议通过；2026-08-24 真实 CloudVisit OpenAI Responses 流式收发通过；真实 Anthropic 待验收）
- [x] C5（AC4）：两个协议请求都包含 system prompt 和完整历史；thinking 开启时分别生成正确参数。（证据：MockTransport 请求体、认证头与 thinking 参数断言通过）
- [x] C6（AC5）：正文按多个增量到达；thinking/reasoning 增量不会进入输出或历史。（证据：双协议 SSE 夹具均只产出 `你`、`好`）
- [x] C7（N3）：两种协议产出相同的上层 RuntimeEvent 序列和错误语义。（证据：双协议 Provider 流式/错误参数化测试及统一 Runtime 测试通过）

## 会话与界面

- [x] C8（AC6）：连续多轮请求携带此前完整上下文；进程重启后历史为空。（证据：运行时历史请求体断言；本地 PTY 中“记住青鸟-42 → 追问”正确回答；实现无持久化路径）
- [x] C9（AC7）：界面具有猫咪 banner、应用名/版本/工作目录、就绪行、对话区、带 `❯` 和占位符的输入框、provider/model 状态栏。（证据：80×24 真实 PTY 与 Pilot 均观察到全部元素）
- [x] C10（AC8）：流式期间为实时纯文本，结束后代码块、列表、强调以 Markdown 定型。（证据：本地 SSE 服务分 6 字符增量；PTY 先逐段显示原文，随后显示项目符号和代码块）
- [x] C11（AC9）：Alt+Enter 插入换行，Enter 提交并清空；流式期间不能重复提交但仍可滚动。（证据：Pilot 键盘测试和延迟流输入锁测试通过）
- [x] C12（AC10）：`/exit` 和 Ctrl+C 均安全退出，终端状态正常。（证据：Pilot `/exit`；两次真实 PTY 分别用 `/exit`、Ctrl+C 退出，均恢复备用屏幕与光标）
- [x] C13（AC11）：错误密钥/模型错误显示区别样式，应用不退出并可继续提交。（证据：`test_http_errors_are_typed_and_secret_is_redacted`、`test_error_is_visible_and_next_submission_is_available` 通过）
- [x] C14（AC12）：请求发出后、首增量前显示实时秒数；完成后显示总耗时。（证据：1.1 秒延迟流测试；真实 PTY 显示 `Imagining… (0s)` 与 `完成 · 1.5s`）
- [x] C15（AC13/N1）：等待和流式期间界面不冻结、可滚动、计时持续更新。（证据：延迟流 Textual Pilot 期间状态与计时持续更新）
- [x] C16（N6/N7）：窄终端布局不溢出，退出后没有残留 raw mode 或错乱显示。（证据：42×16 Pilot 正常；真实 PTY 退出序列恢复备用屏幕和光标）

## 编译、测试与边界

- [x] C17：`python -m compileall -q src tests` 通过。（证据：2026-08-19 命令退出码 0）
- [x] C18：`ruff check src tests` 通过。（证据：2026-08-19 输出 `All checks passed!`）
- [x] C19：全部 pytest 自动化测试通过。（证据：2026-08-19，25 passed）
- [x] C20：本章没有工具调用、持久化、额外 slash 命令、自动重试、运行时 provider 切换或其它越界功能。（证据：实现范围审查，仅包含 config/provider/runtime/tui）

## 端到端场景

- [x] E1：在 tmux 中用单 provider 启动，看到完整 TUI；输入真实请求后观察工具调用、流式回复、计时和最终 Markdown。（证据：2026-08-24 使用临时便携 tmux 3.7b + 本地确定性 SSE，在 pane 中启动完整 TUI，输入“请读取 README.md 后总结项目”，观察 `Read(README.md)`、最终总结、`完成 · 1.4s` 与 Token 统计；Ctrl+C 后 tmux server 自动退出）
- [x] E2：在同一会话先告知“青鸟-42”再追问，模型正确引用；新进程未主动 `/resume` 时使用新会话，不自动继承旧历史。（证据：真实 ConPTY 多轮输出、session 启动/恢复行为测试）
- [x] E3：错误密钥触发 `认证失败（invalid key）`，错误可见、输入框恢复且会话未退出；改正配置并重启后正常流式返回 Markdown。（证据：2026-08-19 真实 ConPTY + 本地 SSE 的 401/200 日志）

## 当前受环境限制的条目

- 真实模型：2026-08-24 已用仅存在于进程内的 CloudVisit 凭据完成 OpenAI Responses 两轮工具对话；未写入 `config.yaml`。真实 Anthropic 尚无可用凭据，不能完成 AC3 的双协议对照。
- tmux：2026-08-24 从 MSYS2 官方仓库临时解压 tmux 3.7b 与 libevent，在真实 pane 中完成 E1；应用、socket 和服务进程已清理，便携文件因安全审查要求显式删除确认而暂留系统临时目录，不修改系统 PATH。
