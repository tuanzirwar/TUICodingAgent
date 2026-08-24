---
name: test
description: 识别项目类型并运行合适的测试
tools: [read_file, glob, bash]
mode: inline
---
先识别项目类型与现有测试入口，再运行范围合适的测试。失败时定位根因并清晰汇报，不要掩盖失败。附加要求：$ARGUMENTS
