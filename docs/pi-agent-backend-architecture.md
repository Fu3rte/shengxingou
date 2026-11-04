---
title: Pi Agent 与后端文件架构
lang: zh
template: doc
theme: paper
---

## 结论

Pi 将模型适配、Agent Loop 和应用会话拆成独立包。本项目可保持 Python DDD 分层，在应用层编排 Loop，在基础设施层接入模型和存储。

本地 Pi 基线为 `b64e6badd6ddefda17fc7f6286c013467186d8a1`。这是源码静态分析，未运行行为测试。

## Pi 的包边界

- `packages/ai/src/`：模型、消息类型、Provider 和流式接口。`providers/` 放不同模型供应商的适配。
- `packages/agent/src/`：通用 Agent 运行时。核心文件为 `agent.ts`、`agent-loop.ts` 和 `types.ts`。
- `packages/coding-agent/src/core/`：编码产品的会话、配置、工具、资源和扩展。`sdk.ts` 负责组装会话。
- `packages/durable/src/`：独立的持久化运行时。它基于 pi-ai 和 Chord，包含 session、storage、tasks 和 harness。

证据：`packages/agent/src/index.ts:1-5`、`packages/coding-agent/src/core/sdk.ts:1-49`、`packages/durable/README.md:1-8`。

## Agent 核心如何拆分

- `agent.ts`：维护模型、消息、工具、运行状态和输入队列，并提供宿主调用入口。
- `agent-loop.ts`：编排模型请求、完整响应、工具执行、下一轮和事件。
- `types.ts`：定义 Agent 消息、事件、工具、回调和配置契约。
- `stream-fn.ts`：提供流式模型调用的注入点。

`runAgentLoop` 接收模型流函数和事件 sink。事件 sink 可等待关键处理完成。证据：`packages/agent/src/agent-loop.ts:34-42,110-135`。

## 应用会话与工具放在哪里

`coding-agent/src/core/sdk.ts` 组装 Agent、模型运行时、配置、资源加载器和会话管理器。

`agent-session.ts` 负责应用会话；`session-manager.ts` 负责会话记录；`model-runtime.ts` 管理模型运行时。`core/tools/` 按能力拆出 `read.ts`、`edit.ts`、`bash.ts` 等实现。

这些属于编码产品能力。本项目只需实现自身明确需要的工具和会话行为。

## 持久化运行时的边界

`durable/src/harness/generation.ts` 管理生成任务；`harness/tool.ts` 管理工具执行；`harness/inbox.ts` 管理输入；`storage/` 提供存储实现。

该包标记为 experimental。其进度更新存在延迟持久化窗口，不能把每次可见进度视为已落盘。见 `docs/agent-runtime/01-pi-runtime-research.md` 的 Deferred progress 章节。

## 对当前 Python 后端的映射

- `backend/src/domain/`：对话、消息、Run、工具调用及状态规则。
- `backend/src/application/`：Agent Loop、工具调用编排及应用服务。
- `backend/src/infrastructure/`：模型 SDK、工具实现与选定的存储适配。
- `backend/src/interfaces/`：FastAPI 路由、请求响应模型与事件传输。

这些是职责建议。当前目录尚无 Python 实现；具体文件按已确定能力建立。前端保持现有 feature-based 结构。

## 文档中的关键约束

- 一个 Run 可包含多个 Turn；完整模型响应后才执行工具。
- 应用设计要求工具默认串行，工具错误终止 Run。
- 确定消息和结果先提交再发布；流式草稿允许丢失。
- 写操作结果未知时标记 interrupted，按业务事实核对。
- 身份和权限来自服务端；工具参数、输出和日志需要校验与脱敏。

以上是 `docs/agent-runtime/02-agent-loop-design.md` 和 `03-tool-design.md` 的项目提案，尚未实现。Pi 自身默认行为与部分提案不同，详见 `01-pi-runtime-research.md`。

## 待确定的实现输入

模型供应商和 Python SDK、工具范围、会话存储、身份权限、部署方式均需明确。`docs/pi-tools-research.md` 使用较早的 Pi commit；其模拟搜索和天气工具只是示例提案。

本次没有修改应用代码，也没有验证模型调用、工具执行或恢复行为。
