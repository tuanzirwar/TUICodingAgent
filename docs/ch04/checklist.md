# TUICodingAgent 第 4 章 Checklist

- [x] C1：多步工具任务无需用户催促即可自然完成。
- [x] C2：纯文本立即自然完成。
- [x] C3：达到 12 次迭代上限后提示并停止。
- [x] C4：连续 3 轮仅未知工具后提示并停止。
- [x] C5：Provider 流错误后应用可继续。
- [x] C6：事件流覆盖文本、工具、用量、进度、完成、错误、取消。
- [x] C7：流式文本实时展示且工具碎片完整收集。
- [x] C8：连续只读并发、副作用串行、结果保序。
- [x] C9：各种停止条件后历史配对合法。
- [x] C10：Esc/Ctrl+C 取消本轮；空闲 Ctrl+C 退出。
- [x] C11：状态栏展示累计输入/输出 Token。
- [x] C12：动态区展示 Agent 迭代轮次。
- [x] C13：`/plan` 仅只读，`/do` 恢复全工具并立即执行。
- [x] C14：OpenAI/Anthropic 多轮、用量和取消语义一致。
- [x] C15：ruff、pytest、compileall 全部通过。

## 端到端

- [x] E1：PTY 中自动完成读取规格、再写摘要文件的多步任务。
- [x] E2：PTY 中进入 `/plan`，确认只读规划，再以 `/do` 执行。
- [x] E3：PTY 中取消正在运行的 Loop，随后继续对话。
- [ ] E4：真实 OpenAI 与 Anthropic 配置对照。（2026-08-24 OpenAI Responses 两轮 Agent Loop 已通过；Anthropic 待验收）
