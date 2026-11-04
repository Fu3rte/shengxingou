# 省心购

项目目前处于搭建阶段，目标是实现包含前后端界面的最小 Agent Loop。

## 技术栈

- 后端：Python 3.14+、FastAPI、Pydantic、Uvicorn，使用 uv 管理依赖。
- 前端：Next.js App Router、React、TypeScript、Tailwind CSS、shadcn/ui 和 Radix UI，使用 npm 管理依赖。

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

### 后端

```bash
cd backend
uv sync --locked
```

当前尚未创建 FastAPI 应用入口，因此后端服务暂无法启动。

### 前端

```bash
cd frontend
npm ci
npm run dev
```

浏览器访问 `http://localhost:3000`。当前首页为空，业务界面尚未实现。

生产构建与启动：

```bash
npm run build
npm run start
```
