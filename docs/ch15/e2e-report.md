# TUICodingAgent 第 15 章端到端验收报告

## 环境

- 日期：2026-08-19
- 平台：Windows PowerShell + Git
- 终端：真实 Windows ConPTY；主机无 tmux/WSL/iTerm2，使用规范指定的 in-process fallback
- 模型端：本地确定性 OpenAI SSE 服务 `tests/ch15_e2e_server.py`
- 仓库：专用临时 Git 仓库 `ch15_e2e_audit`（验收后已删除）

## 真实对话结果

1. 输入“请创建团队进行验收”，模型调用 `TeamCreate(team_name="inproc")`，TUI 显示 `TEAM_CREATED`，config 落盘且后端为 `in-process`。
2. 输入“请启动队员进行验收”，模型调用 `Agent(team_name="inproc", name="bob")`；队员在 `team-inproc+bob` Worktree 调用 `write_file`，主分支未被修改。
3. config 中 bob 从 active 变为 false；Lead 邮箱收到 `[idle] bob: BOB_CHILD_DONE`，TUI 自动发起后续协调轮次。
4. `/team info inproc` 显示 bob 的 ID、后端、状态与 Worktree 路径。
5. 输入“请续派队员进行验收”，模型调用 `SendMessage(to="bob")`；bob 的原 runtime 被恢复，邮箱消息标 read，文件从 `hello` 更新为 `hello\nworld`。
6. 输入“请删除团队进行验收”，模型调用 `TeamDelete(force=true)`，TUI 显示 `TEAM_DELETED`；Team 目录、session 和 Worktree 均消失，`git worktree list` 只剩主仓库。

## 自动化

- `ruff check src tests`：通过。
- `python -m compileall -q src tests`：通过。
- `pytest -q`：190 passed，1 skipped（2026-08-22 复验）；新增 Responses 覆盖后于 2026-08-24 复验为 196 passed，1 skipped。

## 2026-08-19 完整性复验

- 真实 ConPTY 中逐次批准 TeamCreate 与 TeamDelete，确认审批弹窗和继续执行链路正常。
- 确认 bob 的 mailbox 消息被标记为已读，续派后 `team-result.txt` 内容为 `hello\nworld\n`。
- TeamDelete 后 Team 目录、session、Worktree 全部移除，应用 `/exit` 后正确恢复终端状态。
- 确认 pane 队员的邮箱读取已接入每次模型调用前的 reminder provider，并支持完整的同步 `Agent` 工具。
- 通过命令级集成测试确认 tmux/iTerm2 的 spawn、wake、kill 参数正确；该项不替代真实 pane 验收。
- 确认普通邮箱消息的四键落盘格式、结构化消息类型、共享任务状态和失败启动回滚均有自动化保护。
- 真实临时 Git 仓库集成测试确认 in-process 队员只在独立 Worktree 写入，完成后发送 idle，Team 强制删除会清理 Worktree。
- `<team-context>` 已包含当前成员花名册；Pane 队员回复批准 shutdown 后会退出自治循环。

## 2026-08-24 真实 tmux pane 补测

- 临时便携环境：MSYS2 tmux 3.7b + libevent，本地确定性 SSE 服务；不调用外部模型 API。
- `TeamCreate` 实际返回 `backend: tmux`；`Agent` 创建独立 Git Worktree，并启动真实 pane `%0` 中的隐藏 `--team-member` 进程。
- pane 队员完成 `write_file`，Lead 收到 idle；config 记录 `%0`、独立 session 和 Worktree，主分支未被修改。
- `SendMessage(to="bob")` 返回 `delivered: 1`，`tmux send-keys` 唤醒同一 pane；队员会话追加第二轮工具调用，`team-result.txt` 从 `hello` 更新为 `hello\nworld`。
- `TeamDelete(force=true)` 后 tmux server 不存在、Team/队员 session/Worktree 均删除，`git worktree list` 只剩主仓库。
- Windows 便携 MSYS tmux 需设置 `MSYS=noglob`，避免格式串 `#{pane_id}` 被 glob 处理；这是验收运行时设置，不影响项目代码。

## 2026-08-24 同会话 split-window 补测

- 使用 Git for Windows 自带 `winpty` 在专用 socket `tuicodingagent-ac28` 的 pane `%0` 内启动完整 TUICodingAgent TUI，解决 Windows 原生控制台程序进入 MSYS tmux 的 PTY 桥接问题。
- Lead 在 `%0` 调用 `TeamCreate`，随后 `Agent` 实际执行 `tmux split-window`；`tmux list-panes` 同时显示 Lead `%0`（`winpty`）和队员 `%1`（Python `--team-member`），两者处于同一 session。
- `%1` 在独立 Worktree 写入 `hello` 并 idle；Lead `%0` 调用 `SendMessage` 后，`send-keys` 唤醒 `%1`，文件更新为 `hello\nworld`。
- `TeamDelete(force=true)` 后 `%1`、Team 目录和 Worktree 消失，仅剩 `%0`；Lead Ctrl+C 后专用 tmux server 报告 `no server running`。

结论：in-process、tmux 新 session 与严格同会话 `split-window` 端到端链路均通过；仅 iTerm2 后端保留 macOS 实机复验。
