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

在项目根目录启动前后端：

```bash
npm run dev
```

访问 `http://127.0.0.1:3000`。按 Ctrl+C 停止前后端；端口被占用时，先释放端口。

### 后端

```bash
cd backend
uv sync --locked
```

启动终端 Agent：

```bash
uv run python -m src.agent.agent_loop
```

输入问题后按回车。Agent 在需要计算时调用四则运算工具，并输出工具调用与最终回答。计算使用 Decimal 默认精度（28 位有效数字）。输入结束符退出（Windows 使用 Ctrl+Z 后按回车，macOS/Linux 使用 Ctrl+D）。

工具调用循环没有轮次上限，会产生模型调用费用。除零、无效参数和 API 错误会直接终止程序。

运行测试：

```bash
uv run python -m unittest discover -s tests
```

启动本机 API：

```bash
uv run python -m src.dev_server
```

网页对话以明文保存在 `backend/.data/conversations.json`，包含消息和工具结果。每次发送最多请求模型 64 次；停止后不再发起新一轮，已发出的请求可能继续。程序重启时，未完成的任务标记为中断，可手动重试。网页接口仅支持本机单进程运行。

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
