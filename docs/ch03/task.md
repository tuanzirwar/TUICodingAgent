# TUICodingAgent 工具系统 Tasks

## 任务列表

### T1：扩展工具领域模型
**文件：** `models.py`、`tools/base.py`
**步骤：** 实现定义、调用、结果、ProviderEvent、扩展 ChatMessage/RuntimeEvent；提供参数校验和截断帮助函数。
**验证：** 模型不可变、JSON 序列化、错误结果单测。

### T2：实现文件工具
**文件：** `tools/files.py`、`tests/test_tools.py`
**依赖：** T1
**步骤：** 实现 Read/Write/Edit、行号、父目录、唯一匹配和体量上限。
**验证：** AC2–AC4 及大文件测试。

### T3：实现 Bash 工具
**文件：** `tools/shell.py`、`tests/test_tools.py`
**依赖：** T1
**步骤：** 异步子进程、cwd、stdout/stderr/exit code、非零结果、超时 kill、截断。
**验证：** 成功、stderr 非零、超时与长输出测试。

### T4：实现 Glob/Grep 工具
**文件：** `tools/search.py`、`tests/test_tools.py`
**依赖：** T1
**步骤：** 递归 glob、正则检索、二进制跳过、排序、200 项上限。
**验证：** AC6、无结果、非法正则与海量结果。

### T5：实现注册中心
**文件：** `tools/registry.py`、`tools/__init__.py`
**依赖：** T2–T4
**步骤：** 注册六工具、重名保护、查找、双协议定义导出、超时和异常包装。
**验证：** AC1、未知工具、参数/异常/超时结果。

### T6：升级 OpenAI Provider
**文件：** `providers/base.py`、`providers/openai.py`、`tests/test_tool_providers.py`
**依赖：** T1、T5
**步骤：** 发送工具定义；拼接多 index 的 id/name/arguments；序列化调用与结果历史。
**验证：** 请求体与碎片夹具断言。

### T7：升级 Anthropic Provider
**文件：** `providers/anthropic.py`、`tests/test_tool_providers.py`
**依赖：** T1、T5
**步骤：** 发送 schema；解析 tool_use 与 partial_json；序列化 tool_result。
**验证：** 请求体、碎片与 thinking 过滤断言。

### T8：实现单轮工具 Runtime
**文件：** `runtime.py`、`tests/test_tool_runtime.py`
**依赖：** T5–T7
**步骤：** 两阶段请求、顺序执行、事件、回灌、最终正文和第二轮调用上限。
**验证：** AC8、AC9、失败工具续答、多调用顺序和双协议同事件语义。

### T9：实现工具行 TUI
**文件：** `tui/widgets.py`、`tui/app.py`、`app.tcss`、`tests/test_tool_tui.py`
**依赖：** T8
**步骤：** TOOL_STARTED 时挂载工具行，TOOL_FINISHED 更新摘要/样式；长结果截断；保持滚动与恢复。
**验证：** Pilot DOM、失败样式、scrollback 和输入恢复。

### T10：全量验收
**依赖：** T1–T9
**步骤：** 更新 README；运行 ruff/pytest/compileall；本地/真实 API PTY 或 tmux 执行“读文件并总结”。
**验证：** `docs/ch03/checklist.md` 逐项记录证据。

## 执行顺序

`T1 → (T2,T3,T4) → T5 → (T6,T7) → T8 → T9 → T10`

