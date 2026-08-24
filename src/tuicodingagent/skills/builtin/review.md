---
name: review
description: 在独立上下文中审查代码并回流结论
tools: [read_file, glob, grep]
mode: fork
context: recent
---
审查相关代码，重点检查正确性、安全性、可维护性和测试缺口。只报告有证据的问题，并给出文件位置与修复建议。审查范围：$ARGUMENTS
