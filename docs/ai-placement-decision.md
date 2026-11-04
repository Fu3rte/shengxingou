---
title: AI 模块的放置位置
lang: zh
template: doc
theme: paper
---

## 建议

将 AI 放在 `backend/src/ai/`，与现有 DDD 分层并列。当前目标是最小 Agent Loop；同一个 Python 进程、uv 项目和部署单元可以完成它。

## 目录职责

- `backend/src/ai/agent/`：Loop、消息、事件和工具调度。
- `backend/src/ai/models/`：模型 SDK 适配。
- `backend/src/ai/tools/`：Agent 工具及业务能力适配。
- `backend/src/domain/`：领域规则。
- `backend/src/application/`：业务用例及 Agent 运行入口。
- `backend/src/infrastructure/`：业务存储与外部服务。
- `backend/src/interfaces/`：FastAPI 路由及事件传输。

## 边界

AI 通过明确的工具接口调用业务应用服务。入口处组装模型、工具和服务。领域层不依赖 AI、FastAPI 或模型 SDK。

应用服务为每次运行提供服务端身份和权限上下文。工具调用仍由业务服务校验权限。

## 独立部署的条件

当 AI 运行需要独立扩容、隔离进程、独立发布，或供多个服务调用时，可将 `ai/` 提取为独立项目。届时需要明确进程间协议、鉴权、超时和取消语义。

当前项目尚无 Agent 实现；上述目录为建议，未修改现有代码。
