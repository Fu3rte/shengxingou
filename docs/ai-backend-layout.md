---
title: AI 与后端并列的项目架构
lang: zh
template: doc
theme: paper
---

## 项目根目录

- `ai/`：Agent Loop、模型适配和工具调度。
- `backend/`：FastAPI、DDD 业务能力和业务存储。
- `frontend/`：React 界面。
- `docs/`：调研与设计文档。

`ai/` 和 `backend/` 是项目根目录下的同级目录。

## AI 部分

- `ai/src/agent/`：Loop、消息、事件、运行状态与工具调度。
- `ai/src/models/`：模型 SDK 与供应商协议适配。
- `ai/src/tools/`：工具定义和调用业务能力的适配。
- `ai/tests/`：Agent、模型适配和工具测试。

## 后端部分

- `backend/src/domain/`：领域对象和业务规则。
- `backend/src/application/`：业务用例和服务。
- `backend/src/infrastructure/`：数据库和外部业务服务适配。
- `backend/src/interfaces/`：FastAPI 路由和请求响应。
- `backend/tests/`：领域、服务与接口测试。

## 协作路径

前端向后端提交输入。后端调用 AI 运行入口。AI 调用模型，并在需要时通过工具访问后端开放的业务能力。后端将运行事件传递给前端。

业务权限由后端校验。AI 工具收到的身份上下文由服务端提供。

## 连接方式

目录分离本身不决定部署方式。AI 与后端可以作为同一 Python 进程中的两个包，也可以通过服务接口通信。具体连接和依赖管理需要结合部署要求确定。

当前仅提供目录设计；项目文件尚未迁移。
