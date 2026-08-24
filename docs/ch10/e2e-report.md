# TUICodingAgent 第 10 章端到端验收

## 环境

- 平台：Windows PowerShell
- 终端驱动：Windows ConPTY（当前环境未安装 tmux/WSL，使用等价的真实伪终端）
- 模型端：本地确定性 SSE 服务，走完整 OpenAI 流式请求路径

## 结果

- `/help` 展示注册中心内全部 12 条命令；`/status`、`/memory`、`/permission`、`/session` 均即时返回，未访问模型端。
- `/review` 作为普通用户消息发起真实流式回合；固定审查提示与助手回复均写入会话 JSONL。
- `/clear` 清空界面和计数并新建会话；随后 `/resume` 成功恢复旧会话 `20260819-140535-3c3e` 的 2 条消息。
- 真实终端输入 `/` 显示命令菜单，继续输入 `/s` 后只保留 `/session` 与 `/status`；Esc 关闭菜单。Enter 执行、方向键及 Tab 行为另由 Textual Pilot 自动化覆盖。
- `/exit` 正常退出，测试 API 进程已终止。

## 质量门禁

- `python -m pytest`：127 passed，1 skipped（Windows 不支持测试所需符号链接）。
- `python -m ruff check src tests`：通过。
- `python -m compileall -q src tests`：通过。
