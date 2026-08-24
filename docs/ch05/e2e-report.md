# TUICodingAgent 第 5 章端到端报告

## 环境与方法

- Windows PowerShell、Python 3.14.2、Textual 7.5.0。
- 环境没有 tmux/WSL，使用 Windows PTY 启动真实全屏 TUICodingAgent。
- `tests/e2e_sse_server.py` 作为确定性的 OpenAI 兼容流式端点，并返回缓存创建/读取字段。
- 未配置真实 Anthropic API 密钥，因此真实缓存命中项保留未验收。

## 场景结果

1. 在 TUI 输入“环境感知验收”。兼容端点检查第二个 system 内容块确实含工作目录、平台、日期、Git 状态、应用版本和当前模型；回复为“已感知完整运行环境；补充提醒仅作为约束，不会直接复述”。
2. 输入 `/plan` 后执行“多轮规划提示”。界面始终保持 PLAN，只出现 Read 工具；Agent 连续运行 5 次读取并在第 6 轮自然结束。自动化请求捕获进一步确认第 1、5 轮为完整提醒，第 2、3、4、6 轮为精简提醒，持久历史不含标签。
3. 运行 `python tests/cache_smoke.py`，输出 `cache_creation_input_tokens=16 cache_read_input_tokens=8`，证明兼容端点字段被 Provider 解析并由 Runtime 累计暴露。

## 自动化覆盖

- 模块顺序、空槽跳过、插入扩展、稳定字节一致。
- 环境完整性、Git 超时降级和密钥排除。
- OpenAI 稳定前缀与 `cached_tokens`；Anthropic 系统/工具缓存断点及创建/读取字段。
- 字段缺失归零、双重工具约束、reminder 复制注入和 Plan 节奏。
- ch04 的 Agent Loop、取消、历史配对、Provider 错误恢复与 TUI 行为全部回归。
