# 第 15 章验收 Checklist

- [x] AC1–AC5：Team 目录、sanitize/后缀、恢复、删除保护与资源清理。
- [x] AC6：TMUX → iTerm2 → tmux PATH → in-process 的探测顺序。
- [x] AC7–AC9：Team Agent、独立 Worktree、嵌套限制与上下文工具过滤。
- [x] AC10–AC11：共享任务落盘、双向依赖和 ready 计算。
- [x] AC12–AC15：单播/广播、10 路并发无丢失、10 秒 stale lock 恢复。
- [x] AC16–AC20：每次模型调用前的 incoming reminder、idle、续派、Plan 权限批准并恢复 Lead 当前权限模式。
- [x] AC21–AC22：Coordinator 双开关、白名单和状态标签。
- [x] AC23–AC24：2026-08-24 使用临时便携 tmux 3.7b 实跑；spawn 返回真实 pane `%0`，pane 内隐藏队员完成 `write_file` 并 idle；`SendMessage` 通过 `send-keys` 唤醒同一 pane，文件由 `hello` 更新为 `hello\nworld`。
- [x] AC25–AC26：in-process 共享 Manager/独立 cwd，`/team` 四个子命令。
- [x] AC27：`ruff check src tests`、`compileall`、全量 pytest 通过（2026-08-24：196 passed，1 skipped）。
- [x] AC28：2026-08-24 使用便携 tmux 3.7b + `winpty` 完成严格同会话实跑；Lead `%0` 内运行完整 TUI，`Agent` 通过 `split-window` 创建队员 `%1`，首轮写入 `hello`，`SendMessage` 唤醒 `%1` 后追加 `world`，`TeamDelete` 删除 `%1`、Team 与 Worktree，Lead Ctrl+C 后专用 tmux server 退出。
- [x] AC29：ConPTY 实跑创建、派出 bob、idle、消息续派、Worktree 写入及删除。
- [x] AC30：自动化验证 Coordinator 下 `write_file` 不在允许工具中、`bash` 保留、状态栏显示标签。

## 2026-08-22 补充审计

- [x] tmux/iTerm2 的 spawn、wake、kill 命令参数由自动化测试逐项断言，包括隐藏的 `--team-member` 启动参数；tmux pane 已实跑，iTerm2 保留 macOS 实机复验。
- [x] 普通邮箱消息严格只落盘 `from` / `text` / `timestamp` / `read` 四个键；五种结构化消息类型和共享任务四种状态均由 schema 与运行时双重校验。
- [x] Team 队员启动阶段统一回滚，运行时构造或后端 spawn 失败不会遗留成员注册、session 或 Worktree。
- [x] `<team-context>` 包含当前成员花名册；Pane 队员批准 shutdown 后退出自治循环。
- [x] 真实 Git Worktree 集成测试覆盖 in-process 队员隔离写入、idle 通知和 Team 删除清理。
