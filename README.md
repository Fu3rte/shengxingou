# 省心购

基于 Python Agent Loop 的本地对话应用，支持会话管理、流式展示、工具调用和待办事项。

## 技术栈

- 后端：Python 3.14+、FastAPI、Pydantic、Uvicorn，使用 uv 管理依赖。
- 前端：Next.js App Router、React、TypeScript、Tailwind CSS、shadcn/ui 和 Radix UI，使用 npm 管理依赖。
- Agent 评测：Langfuse（尚未接入）。

## 项目目录

- `backend/src/api.py`：本机 HTTP 和 SSE 接口。
- `backend/src/ai/`：模型和供应商适配。
- `backend/src/agent/`：Agent Loop、会话持久化和工具。
- `backend/tests/`：后端测试。
- `frontend/src/app/`：Next.js 路由和布局。
- `frontend/src/features/`：按功能组织的前端代码。
- `frontend/src/components/`：共享 UI 组件。
- `frontend/src/lib/`：前端共享工具。
- `docs/`：Agent 和工具设计调研。
- `.agents/skills/`：项目级 Pi skills。

## 启动方式

环境要求：Python 3.14+、uv、Node.js 20.9+ 和 npm。

首次安装依赖，在项目根目录运行：

```bash
npm run setup
```

在项目根目录启动前后端：

```bash
npm run dev
```

访问 `http://127.0.0.1:3000`，在页面配置模型、API Key 和可选的 OpenAI 兼容接口地址。也可将 `backend/.env.example` 复制为 `backend/.env`，填写 `OPENAI_MODEL`、`OPENAI_API_KEY` 和可选的 `OPENAI_BASE_URL`。页面保存的非空配置优先生效。

使用 `Ctrl+C` 停止前后端。更新服务入口后，需要停止旧开发进程并重新启动。

配置保存在本机 `backend/.data/config.json`，API Key 以明文保存；会话和执行记录保存在 `backend/.data/sessions/`。`backend/.data/` 和 `backend/.env` 已加入 Git 忽略规则，请保护本机文件。

### 后端

```bash
cd backend
uv sync --locked
```

运行测试：

```bash
uv run python -m unittest discover -s tests
```

仅在本机启动 API（单进程）：

```bash
uv run uvicorn src.api:app --host 127.0.0.1 --port 8765 --no-access-log
```

`backend/src/agent/runtime.py` 提供 Agent 运行接口，`backend/src/agent/agent_loop.py` 实现循环，`backend/src/api.py` 提供会话、消息、停止、删除确认和执行事件接口。执行事件通过 SSE 推送，断线后支持按事件游标恢复。

各会话的消息和运行记录相互隔离，所有会话共享同一个待办文件。删除待办需要确认；停止请求在当前模型响应或工具执行结束后生效。计算器执行实际计算；天气和搜索使用本地样例数据。

### 前端

```bash
cd frontend
npm ci
npm run dev -- --hostname 127.0.0.1
```

浏览器访问 `http://127.0.0.1:3000`。

生产构建与启动：

```bash
npm run build
npm run start -- --hostname 127.0.0.1
```
