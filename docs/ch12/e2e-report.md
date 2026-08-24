# TUICodingAgent 第 12 章端到端验收报告

## 环境

- 日期：2026-08-19
- 平台：Windows PowerShell
- 终端：真实 ConPTY 会话；本机未安装 tmux/WSL，使用 Windows 等价伪终端完成交互验收
- 模型端：本地确定性 OpenAI SSE 测试服务

## 对话与结果

1. `/hooks` 正确列出 5 条规则，按事件分组显示名称、`only_once`、`async` 和来源。
2. 输入“首轮验收”，`SessionStart` prompt 注入生效，模型回复 `SESSION_PROMPT_OK`。
3. 输入“Hook工具拦截验收”，模型调用 `write_file`；`PreToolUse` 返回 `blocked by hook`，模型收到失败工具结果并回复 `PRETOOL_BLOCK_OK`，目标文件不存在。
4. 输入“异步后置验收”，模型调用 `read_file` 后立即继续并回复 `POSTTOOL_CONTINUED_OK`，异步 Hook 最终生成 `async.ok`。
5. 输入 `please delete file`，界面显示 `[hook prompt-guard] prompt contains delete keyword`；模拟 API 未收到请求，会话 JSONL 中不存在该输入。
6. 执行 `/clear` 后输入“重置验收”，模型回复 `CLEAR_SESSION_OK`；`only_once` 日志行数由 1 变为 2，证明新会话已重置执行状态。

## 文件与会话核验

- `blocked.txt`：不存在
- `async.ok`：存在
- `once.log`：2 行
- 会话记录包含 `SESSION_PROMPT_OK`、`PRETOOL_BLOCK_OK`、`POSTTOOL_CONTINUED_OK`、`CLEAR_SESSION_OK`
- 被拦截的 `please delete file` 不在会话记录中

结论：第 12 章端到端验收全部通过。
