# TUICodingAgent 总体验收 Checklist

本清单汇总 `docs/ch01` 至 `docs/ch15` 的章节验收；细项和运行证据以各章
`checklist.md`、`e2e-report.md` 为准。最终逐章证据映射见
[`docs/completion-audit.md`](docs/completion-audit.md)。

## 章节状态

- [x] ch01：流程治理、命名迁移和章节文档完整。
- [x] ch02：双协议 Provider、流式多轮对话与基础 TUI；真实 OpenAI Responses 与 tmux E1 已通过，Anthropic 项见“外部环境待验收”。
- [x] ch03：核心工具、双协议工具调用和结果回灌；真实双 Provider 项见“外部环境待验收”。
- [x] ch04：Agent Loop、多步执行、取消、并发与 Plan/Do。
- [x] ch05：模块化提示、动态 reminder、缓存布局和用量解析；真实 Anthropic 缓存项见“外部环境待验收”。
- [x] ch06：危险命令、路径沙箱、规则、权限模式和人在回路。
- [x] ch07：stdio/HTTP MCP、工具桥接、权限与生命周期。
- [x] ch08：工具结果落盘、上下文压缩、熔断与恢复。
- [x] ch09：项目指令、会话持久化/恢复和长期记忆。
- [x] ch10：统一斜杠命令、帮助和补全菜单。
- [x] ch11：Skill 加载、执行、隔离、安装和内置模板。
- [x] ch12：Hook 配置、匹配、动作、生命周期和失败隔离。
- [x] ch13：SubAgent、Fork、后台任务、通知和续派。
- [x] ch14：Git Worktree 生命周期、隔离、保护、恢复与 stale sweep。
- [x] ch15：Team、共享任务、邮箱、三种后端、续派、Plan 审批和 Coordinator；真实 tmux spawn/wake/kill 与同会话 split-window 已通过，iTerm2 项见“外部环境待验收”。

## 本机自动化与端到端

- [x] `ruff check src tests` 通过。
- [x] `python -m compileall -q src tests` 通过。
- [x] 全量 pytest 通过（196 passed、1 skipped）；唯一 skip 是当前 Windows 权限不允许创建符号链接，Windows Junction 降级已有测试。
- [x] 使用 Textual Pilot、真实 Git、stdio/HTTP 本地服务和 Windows ConPTY 完成不依赖外部凭据的端到端场景。
- [x] in-process Team 真实 Git Worktree 生命周期覆盖隔离写入、idle、续派和清理。
- [x] tmux/iTerm2 spawn、wake、kill 命令与隐藏 CLI 参数由自动化测试验证。
- [x] 真实 tmux pane 完成 TUI 对话、Team 队员 spawn、idle、SendMessage 唤醒、二次写入与 kill/Worktree 清理。

## 外部环境待验收

- [x] 使用真实 OpenAI 兼容 Responses 服务完成流式 FC、工具执行、reasoning item 重放、结果回灌、续答和 Token 用量验收。
- [ ] 使用可用 Anthropic 凭据完成 ch02–ch05 的真实协议对照和 Anthropic prompt cache 验收。
- [x] 在真实 tmux pane 内完成 ch02 E1，并完成 ch15 AC23、AC24 的 pane 启动、唤醒及完整队员对话验收。
- [x] 在真实 tmux 同一会话中完成 ch15 AC28：Lead `%0`、队员 `%1`、唤醒续写、删除 pane/Team/Worktree 与退出清理。
- [ ] 在 macOS iTerm2 环境复验 iTerm2 pane 后端。

当前 Windows 主机没有可运行的 WSL 发行版或 iTerm2；临时便携 tmux 已完成可执行验收，服务与 socket 已清理，便携文件待显式确认删除。
现有凭据仅覆盖 OpenAI Responses 兼容服务，不覆盖 Anthropic。因此剩余条目保持未勾选，
不用 Mock 或 ConPTY 冒充真实外部环境验收。
