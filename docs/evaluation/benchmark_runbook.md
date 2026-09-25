# TUICodingAgent Benchmark Runbook

所有报告均由脚本运行生成 JSON、CSV 和 Markdown；结果目录中的数值不在源码中预置。

## 环境标签

- `live-provider`：真实外部模型请求。必须有有效凭据。
- `synthetic-deterministic`：只验证长会话、压缩、工具、存储和 trace 稳定性，不能用于声称真实模型质量或网络 TTFT。
- `local-deterministic-selector`：本地 MCP Schema 选择器，不代表在线 LLM 工具选择能力。
- `local-async-scheduler`：本地 1/2/4 Agent 调度实验，不代表跨机器 Agent 性能。

当前三个 provider 均返回鉴权失败，因此 8 小时运行明确使用
`synthetic-deterministic`。恢复有效凭据后，去掉 `--synthetic` 并指定
`--provider-index` 可重复执行真实模型版本。

## 命令

```powershell
# 8 小时连续会话
python -m tuicodingagent.benchmark --synthetic --config config.yaml `
  --output docs/evaluation/results/continuous_8h --duration 28800 --interval 30

# MCP 10/50/100/200
python -m tuicodingagent.eval.mcp_lazy_benchmark `
  --output docs/evaluation/results/mcp_lazy --repeats 5

# 上下文压缩
python -m tuicodingagent.eval.context_compression_benchmark `
  --output docs/evaluation/results/context_compression

# 权限、Multi-Agent、工具故障注入
python -m tuicodingagent.eval.permission_benchmark --workspace . `
  --output docs/evaluation/results/permission
python -m tuicodingagent.eval.multi_agent_benchmark `
  --output docs/evaluation/results/multi_agent --repeats 10
python -m tuicodingagent.eval.tool_failure_benchmark `
  --output docs/evaluation/results/tool_failure --trials 20
```

## 长会话指标

报告包含 duration、turn_count、LLM token 总量与分位数、压缩前后 context、
压缩次数、工具输出 raw/retained/offloaded tokens、JSONL/artifact/summary/memory
写入量、估算请求响应流量、turn latency 与 TTFT 分位数。

工具原始输出超过预算时写入受管 artifact。模型可通过 `read_artifact` 和
`search_artifact` 恢复内容。代码修改完成后，verification loop 依次执行 syntax、
lint、test 和 diff check，并受步骤数及超时限制。
