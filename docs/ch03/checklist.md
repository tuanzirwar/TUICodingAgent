# TUICodingAgent 第 3 章 Checklist

- [x] C1（AC1）：注册中心含六工具、按名可查，双协议请求体含六定义。（`test_default_registry_has_six_tools_and_both_protocol_schemas`、双 Provider 请求体测试）
- [x] C2（AC2）：Read 返回带行号内容；不存在文件为结构化错误。（文件工具单测与 PTY E2）
- [x] C3（AC3）：Write 创建/覆盖文件及嵌套父目录。（`test_read_write_and_edit_file_contract`）
- [x] C4（AC4）：Edit 对 1/0/多匹配返回正确结果和匹配数。（同上）
- [x] C5（AC5/N1）：Bash 返回三类输出；超时终止，界面/会话不挂死。（成功、退出码 3、0.05 秒超时与长输出单测）
- [x] C6（AC6）：Glob 返回匹配路径；Grep 返回文件/行/内容。（205 文件搜索与非法正则单测）
- [x] C7（AC7）：双协议工具名和 JSON 碎片组装完整，多调用按 index 区分。（`test_tool_providers.py`）
- [x] C8（AC8）：读文件并总结完成调用→执行→回灌→最终正文。（Runtime 双协议测试与 PTY E1）
- [x] C9（AC9）：第二阶段再调用工具时不执行，发出单轮上限提示。（`test_second_stage_tool_call_hits_limit_and_is_not_executed`）
- [x] C10（AC10/N3）：双协议相同任务产生一致上层事件和结果。（参数化 Runtime 测试）
- [x] C11（AC11）：TUI 显示 `● 工具名(关键参数)`、摘要、失败/截断样式并保留 scrollback。（Pilot DOM 测试与 PTY E1/E2）
- [x] C12（AC12/N4）：不存在文件、非零命令、无匹配编辑均回灌错误，应用可继续。（工具、Runtime 单测与 PTY 后续对话）
- [x] C13（AC13/N5）：大文件、长输出、海量 glob/grep 均被截断。（2000 行/10 万字符/200 项上限单测）
- [x] C14（N2）：流式和工具执行期间 TUI 可响应、计时和滚动正常。（Pilot 与真实 PTY 验证）
- [x] C15（N6）：工具请求、结果、错误与测试日志不泄露 API 密钥。（认证错误脱敏测试；E2E 服务仅记录方法、路径、状态码）
- [x] C16：ruff、pytest、compileall 全部通过。（38 passed）

## 端到端

- [x] E1：在 tmux/PTY 启动并请求“读取 README.md 后总结”，观察工具行、结果摘要和最终总结。（Windows PTY）
- [x] E2：请求不存在文件，观察结构化错误回灌和模型纠正答复，随后会话仍可继续。（Windows PTY）
- [ ] E3：分别以 OpenAI 和 Anthropic 真实配置运行同一工具任务。（2026-08-24 OpenAI Responses 已通过；Anthropic 待验收）
