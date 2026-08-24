# TUICodingAgent 第 14 章端到端验收报告

## 环境

- 日期：2026-08-19
- 平台：Windows PowerShell + Git
- 终端：真实 ConPTY；本机无 tmux/WSL，使用 Windows 等价伪终端
- 仓库：专用临时 Git 仓库，不改变项目自身版本控制状态
- 模型端：本地确定性 OpenAI SSE 服务

## 手动 Worktree

1. `/worktree create manual-e2e` 创建 `.tuicodingagent/worktrees/manual-e2e` 和 `worktree-manual-e2e`。
2. `/worktree list` 正确显示名称、路径和分支；`/worktree enter manual-e2e` 激活显式 cwd。
3. 真实对话“手动目录写入验收”触发 `write_file("manual.txt")`，返回 `MANUAL_WRITE_OK`。主目录不存在该文件，Worktree 中内容为 `MANUAL_ONLY`。
4. `/worktree exit --remove` 因未提交修改返回“拒绝删除”；追加 `--discard` 后目录和分支成功删除。

## SubAgent 自动隔离

1. 真实对话“隔离子任务验收”触发 `Agent(subagent_type=isolated-e2e)`。
2. 子 Agent 在随机临时 Worktree `agent-a7d4c692` 中调用 `write_file`，返回 `ISOLATED_CHILD_OK`；主 Agent 收到保留路径后返回 `ISOLATION_PARENT_OK`。
3. 主目录 `server.py` 保持 `VALUE = "main"`；隔离副本为 `VALUE = 'isolated'`，`git status --porcelain` 显示仅副本的 `server.py` 被修改。
4. 有变更的临时 Worktree 按规则保留并回流路径/分支；验收结束后专用仓库整体安全清理。

结论：第 14 章端到端验收全部通过。

## 2026-08-22 Windows 完整性复验

- 验证根目录 `config.yaml`、`.tuicodingagent/config.yaml` 与 settings 文件会复制到新 Worktree，Pane 队员切换 cwd 后仍能加载同一项目配置。
- 验证 `.husky` hook 路径初始化、`node_modules` 大目录链接和 `worktreeinclude` 配置；Windows 无符号链接权限时使用目录 Junction，仍保持共享而非复制。
- 验证 stale sweep 只删除安全的临时 Worktree；有未提交改动、未推送 commit、当前 session、手动 Worktree和普通非 Worktree 目录均保留。
- 全量回归：`ruff check src tests`、`compileall` 通过；2026-08-22 为 190 passed、1 skipped，新增 Responses 覆盖后于 2026-08-24 为 196 passed、1 skipped。
