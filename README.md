# 省心购

项目目前处于搭建阶段，目标是实现包含前后端界面的最小 Agent Loop。

## 技术栈

- 后端：Python 3.14+、FastAPI、Pydantic、Uvicorn，使用 uv 管理依赖。
- 前端：Next.js App Router、React、TypeScript、Tailwind CSS、shadcn/ui 和 Radix UI，使用 npm 管理依赖。
- Agent 评测：Langfuse（尚未接入）。

## 项目目录

- `backend/src/ai/`：模型和供应商适配。
- `backend/src/agent/`：Agent Loop 和工具。
- `backend/src/domain/`：业务领域规则。
- `backend/src/application/`：业务应用服务。
- `backend/src/infrastructure/`：业务基础设施。
- `backend/src/interfaces/`：业务接口。
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

将 `backend/.env.example` 复制为 `backend/.env`，填写 `OPENAI_API_KEY`、`OPENAI_MODEL` 和需要时使用的 `OPENAI_BASE_URL`。

在项目根目录运行 `npm run dev`，然后访问 `http://127.0.0.1:3000`。模型和 API Key 也可在页面配置，保存在本机 `backend/.data/config.json`。会话及执行记录保存在 `backend/.data/sessions/`。

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

`backend/src/agent/agent_loop.py` 提供 Agent Loop 接口，会话和执行记录保存在 Session JSONL 文件中。

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
