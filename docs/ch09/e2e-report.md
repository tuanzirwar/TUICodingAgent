# TUICodingAgent 第 9 章端到端报告

日期：2026-08-19

## 环境与方法

- Windows 主机无 tmux/WSL，使用真实 Windows ConPTY 启动全屏 TUI。
- 为验证实际磁盘写入，工作目录使用可写临时目录；模型使用本地确定性 OpenAI 兼容 SSE 服务。
- 服务和 TUI 均以真实独立进程运行，完成后按精确 PID 清理服务。

## 验收过程

1. 首次启动加载 `TUICODINGAGENT.md`，输入“请记住项目验收代号是青鸟-42”。主回复在约 0.6 秒完成，记忆更新随后异步发起。
2. 磁盘生成项目笔记 `project_knowledge_acceptance_code.md` 及 `MEMORY.md`；索引含“验收代号 — 项目验收代号是青鸟-42”。
3. 输入“指令记忆验收”，确定性服务检查系统提示，同时看到项目指令标记和更新后的记忆索引，回复“项目指令和长期记忆索引均已注入”。
4. 首次会话 JSONL 的 ID 为 `20260819-134752-19ba`，两轮对话形成 4 行合法 JSON，角色顺序为 user/assistant/user/assistant。
5. 退出并重新启动，输入 `/resume`。列表显示标题、相对时间、模型和文件大小；Enter 后显示“已恢复会话 20260819-134752-19ba，共 4 条消息”。
6. 输入“恢复后继续”，TUI 正常回复；原会话 JSONL 增至 6 行，新增 user/assistant 写入原文件，而新启动时创建的空会话仍保留。

## 自动化结果

- `python -m pytest -q`：121 passed，1 skipped。
- `python -m ruff check .`：通过。
- `python -m compileall -q src tests`：通过。
- 唯一跳过项为 Windows 无法创建测试符号链接，与本章功能无关。
