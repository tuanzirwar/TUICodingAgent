# TUICodingAgent 第 11 章端到端验收

## 环境

- 平台：Windows PowerShell
- 终端驱动：Windows ConPTY（当前环境没有 tmux/WSL，使用真实伪终端等价验收）
- 模型端：本地确定性 OpenAI SSE 服务，端口 8766

## 真实对话结果

- 普通请求“目录验收”返回 `CATALOG_ONLY_OK`：请求中存在 Available Skills 名称/说明和 `load_skill` 指引，不含项目 Skill 的 `E2E-SOP-V1` 正文。
- `/test first` 返回 `INLINE_V1_OK`：inline Skill 已激活，完整 SOP 与参数进入环境上下文。
- 运行中把项目 Skill 改为 `E2E-SOP-V2`，再次执行 `/test second` 返回 `INLINE_V2_OK`，证明调用期热重读生效。
- `/review` 显示独立上下文运行状态并回流 `REVIEW_FORK_OK`，主会话 JSONL 持久化了 Skill 结果。
- `/clear` 后发送“清理验收”返回 `CLEAR_OK`，新请求不再包含 Active Skills。
- `/exit` 正常退出，本地 SSE 服务已终止。

## 自动化质量门禁

- `python -m pytest`：136 passed，1 skipped（Windows 不支持测试所需符号链接）。
- `python -m ruff check src tests`：通过。
- `python -m compileall -q src tests`：通过。

远程 skills.sh 安装按其官方 `/api/v1/skills/{source}/{skill}` 文件树接口实现，并与 GitHub tree、raw URL 一并由 MockTransport 覆盖。
