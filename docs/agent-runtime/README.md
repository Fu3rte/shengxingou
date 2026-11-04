---
title: 省心购 Agent Runtime
lang: zh
template: doc
theme: paper
---
## 设计入口

重点是 Agent Loop 和 Tool。
以 Pi 的 loop/tool 契约为参考，面向 Python/FastAPI 后端设计。
本目录是设计提案，运行机制尚未实现。

## 文档

- [Pi 源码调研与固定证据](01-pi-runtime-research.md)
- [Agent Loop 设计](02-agent-loop-design.md)
- [Tool 设计](03-tool-design.md)
- [验证、取舍与待决策项](04-validation-and-decisions.md)

## Agent Loop

一次输入对应一个 run。
每个 turn 包含一次模型响应及工具调用。
完整响应提交后才进入工具执行。
工具结果提交后，再决定继续或结束。

设计覆盖准入去重、上下文、队列、事件和预算。
取消传播到模型及真实 I/O。
未知写结果进入 interrupted，等待业务核对。

## Tool

统一入口负责 Schema、权限、审批和执行。
默认串行；独立读批次允许并行。
执行前记录 intent，执行后提交 result。
业务写操作绑定稳定的 operationId。

工具错误终止 run。
服务端身份、资源权限和审批不能取自模型文本。
输出的模型、程序和 UI 通道均需脱敏。

## 证据与限制

三个 fresh-context 子代理完成并行研究。
基线是 Pi commit b64e6bad，相关包标记为 1.5.0。
当前 HEAD 还包含 Unreleased 修改。
工具参数由 pi-schema 校验，默认包含类型转换。
输出校验和持久化屏障需要应用明确实现。

项目已有 Python/FastAPI 和 React 依赖配置。
后端分层目录尚未发现 Python 实现或测试。
具体模型 SDK、业务工具和存储尚未确定。
本次仅验证文档产物；行为测试尚未执行。
