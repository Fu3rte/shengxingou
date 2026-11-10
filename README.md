# 省心购

基于 Python Agent Loop 的本地对话应用，支持会话管理、流式展示、工具调用、待办事项和对话上下文压缩。

## 技术栈

- 后端：Python 3.14+、FastAPI、Pydantic、Uvicorn，使用 uv 管理依赖。
- 前端：Next.js App Router、React、TypeScript、Tailwind CSS、shadcn/ui 和 Radix UI，使用 npm 管理依赖。

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

访问 `http://127.0.0.1:5143`，在页面配置模型、API Key 和可选的 OpenAI 兼容接口地址。也可将 `backend/.env.example` 复制为 `backend/.env`，填写 `OPENAI_MODEL`、`OPENAI_API_KEY` 和可选的 `OPENAI_BASE_URL`。页面保存的非空配置优先生效。

使用 `Ctrl+C` 停止前后端。更新服务入口后，需要停止旧开发进程并重新启动。

配置保存在本机 `backend/.data/config.json`，API Key 以明文保存；会话和执行记录保存在 `backend/.data/sessions/`，待办数据保存在该目录的 `.todo/` 中。`backend/.data/` 和 `backend/.env` 已加入 Git 忽略规则，请保护本机文件。

API 限制本机访问，并允许 `localhost:5143` 和 `127.0.0.1:5143` 来源发起请求。API 没有身份验证，本机程序仍可访问。

### 后端

```bash
cd backend
uv sync --locked
```

运行测试：

```bash
uv run pytest
```

仅在本机启动 API（单进程）：

```bash
uv run uvicorn src.api:app --host 127.0.0.1 --port 8765 --no-access-log
```

`backend/src/agent/runtime.py` 提供 Agent 运行接口，`backend/src/agent/agent_loop.py` 实现循环，`backend/src/api.py` 提供会话、消息、停止、删除确认和执行事件接口。执行事件通过 SSE 推送，断线后支持按事件游标恢复。

各会话的消息、运行记录和待办事项相互隔离。删除待办需要确认；停止请求在当前模型响应或工具执行结束后生效。运行期间发送的消息会排队，供后续模型轮次处理。对话上下文达到阈值且存在可压缩的历史记录时，Agent 会调用模型生成摘要。

计算器执行实际计算；天气和搜索使用本地样例数据，不联网。文件工具可读取、覆盖或追加后端进程有权限访问的文件，支持 UTF-8 文本和 Base64 编码的二进制内容。文件写入无需确认，请谨慎使用可信模型和输入。

### 前端

```bash
cd frontend
npm ci
npm run dev -- --hostname 127.0.0.1 --port 5143
```

浏览器访问 `http://127.0.0.1:5143`。

生产构建与启动：

```bash
npm run build
npm run start -- --hostname 127.0.0.1 --port 5143
```
