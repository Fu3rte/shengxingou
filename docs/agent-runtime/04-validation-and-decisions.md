# 验证、取舍与待决策项

## 已确认事实

backend/pyproject.toml:1–9 声明 Python >=3.14、FastAPI 和 Uvicorn。
backend/src/ 已有 application、domain、infrastructure、interfaces 分层目录。
本次限定发现中，backend/src/ 和 backend/tests/ 尚无 Python 文件。
frontend/package.json 声明 React、TypeScript 和 Vite；其 test 脚本尚无可执行测试。
skills-lock.json 只记录 Langfuse skill 安装来源。

本次仅创建研究与设计材料。运行机制尚未实现。

## 设计决策

### 复用边界

本项目以 Pi agent-core 的状态机为行为参考，保持现有 Python/FastAPI 服务端。
模型访问、loop、业务工具各自有明确责任边界。
Pi 当前 HEAD 为 b64e6badd6ddefda17fc7f6286c013467186d8a1，相关包标记为 1.5.0。
HEAD 包含 Unreleased 修改；发布包的实际契约尚未核对。

选择理由：工具调度、输入队列、流式事件和 turn 生命周期已有直接实现与单测。
应用补充认证、预算、确定结果持久化和副作用收据。
业务服务负责写操作事务及幂等。

### 可复用方案的取舍

agent-core：

- 优点：范围小，loop 和 tool hook 易于嵌入。
- 成本：准入持久化、重启中断处理和业务幂等由应用提供。
- 风险：before hook 修改参数后不二次校验；抛错默认转换为可供模型消费的工具错误。
- 校验：当前使用 pi-schema，参数默认先转换后校验；所读 loop 不自动校验 outputSchema。
- 集成：适合 TypeScript 宿主直接嵌入；本项目参考其契约，尚未建立跨语言服务。

durable：

- 优点：生成与工具是持久任务，支持执行意图、结果、恢复和结构化所有权。
- 成本：引入 Session、registry、task、document 和 Chord 的完整约束。
- 风险：README 明确 experimental；一个存储由单进程拥有，包自身没有跨进程存储锁。
- 校验：beforeTool 后二次校验；非法结构化输出对模型和嵌套调用采取不同处理。
- 一致性：deferred progress 默认可领先存储约 1 秒；可见草稿不能作为确定业务事实。
- 升级：存储和 pi.live 曾发生迁移兼容性变化，须验证备份、升级和回滚路径。
- 预算：maxTurns 按工具轮计数，不能用作全部模型请求的硬上限。
- 适用证据门槛：产品明确要求崩溃后自动续跑，且接受 API 变动及存储所有权治理。

本提案选定 Python 服务端实现路径与显式中断语义。
Pi agent-core 提供 loop 行为证据，durable 提供意图记录和恢复设计证据。
二者均未列为项目已采用依赖；运行时设计仍需具体模型 SDK 和业务协议支撑。
现有 Python 分层目录是接入位置线索，尚无具体调用方需要迁移。

### 一致性与并发

确定消息和工具结果先持久化再发布。
草稿属于临时展示，重连允许丢弃。
默认工具串行；独立读批次可以并行。
重放默认禁止，未知写状态进入 interrupted。

## 本次交付检查

已验证五份 Markdown 可读取、索引指向的文档存在、loop/tool 关键契约齐全。
可视化入口 overview.html 已生成，渲染器报告零警告。
已检查 HTML 标题与闭合标签，文件完整可读。
Pi checkout 的 HEAD 已确认，复核时工作区干净。
本次只更新本目录文档与可视化页面，未修改 Pi 或项目应用代码。
未进行浏览器交互、Mermaid 渲染或 runtime 行为测试。

## 最小验证集

以下均为实现验收要求，当前执行状态为未运行。

### Loop

- 无工具响应：只有一次模型请求，一个 run 终态。
- 工具响应：assistant 先提交，工具后执行，结果提交后下一轮读取。
- length 截断：即使参数可解析，业务调用次数仍为零。
- error/aborted：没有额外工具或模型调用。
- finishTurn=end：停止消费队列。
- 显式 continue：自然后继请求满足该决定，不能重复增加请求。
- steering：在安全边界接入；不能被误当成取消。
- follow-up：由宿主持久 inbox 在原 run 终态后准入新 run；不会插入当前工具批次。
- awaited sink：assistant 持久化完成前不执行工具；低层 EventStream 不被误作持久化屏障。
- 监听者存储失败：不发布成功、不重复进入失败写入路径，实例停止准入。
- 同 conversation 并发请求：只接纳一个 active run。
- 重复 requestId：返回相同 run；不同输入复用同键被拒绝。
- 达到 turn/call/deadline 限制：不会启动下一项工作。
- 慢客户端：loop 不被无限阻塞，重连可以恢复确定消息。

### Tool

- 未知、未提供或已下线工具：不执行。
- Schema 不符：不执行；hook 改变最终参数后仍校验。
- 严格输入：数字字符串、分数转整数、optional null 均按项目原始参数策略处理。
- 校验错误含敏感参数：模型、UI、日志和审计均不泄露原始参数。
- 同名工具：应用启动拒绝重复注册；内部调用同样检查权限。
- 伪造 principal/tenantId：无法越权，执行侧只用宿主主体。
- 审批过期或参数摘要不一致：不执行。
- 混合读写批次：整轮串行。
- 独立读并行：结束事件按完成顺序，结果消息按调用顺序。
- 工具错误：run 失败；无自动参数修复或副作用重试。
- 输出 Schema 不符：最终后处理之后校验并显式失败，原始敏感信息不进入任何输出通道。
- Pi 对照：agent-core 的输出声明不等于校验；durable 的模型/嵌套输出违约差异有独立测试。
- 超时：真实 I/O 收到取消；未知写结果保留 interrupted。
- 同 callId 不同参数：拒绝；同业务键不同参数：业务层拒绝。
- 写成功、收据写前崩溃：恢复查询相同 operationId，写次数保持一次。
- intent 后进程退出：unsafe 调用不自动重放。
- 存储失败：不发布成功结果，run 停止。
- 崩溃与断电：分别验证确定收据与临时进度的丢失边界。
- 若采用 durable 存储：验证迁移和回滚兼容性，禁止直接假定旧版本可读新存储。
- 权限在重启前被撤销：恢复执行被拒绝。

使用可控的本地测试依赖验证顺序、取消及崩溃注入。
另需在真实业务集成环境验证权限和幂等协议。
未进行模型调用、供应商付费请求或生产数据写入。

## Pi 对照测试定位

源码前缀：D:/Repository/pi/。

- packages/agent/test/agent-loop.test.ts:305–401：完整工具调用链。
- packages/agent/test/agent-loop.test.ts:456–588：截断禁执行、hook 后参数行为。
- packages/agent/test/agent-loop.test.ts:668–761：并行完成顺序与结果顺序。
- packages/agent/test/agent-loop.test.ts:867–1107：整轮串行覆盖规则。
- packages/agent/test/agent-loop.test.ts:1108–1403：finishTurn 和继续请求。
- packages/agent/test/agent-loop.test.ts:1732–1963：terminate 全批规则。
- packages/durable/test/harness-tools.test.ts:292–327,498–543：调用调度及 hook 后校验。
- packages/durable/test/harness-tools-recovery.test.ts:109–310：unsafe 中断、双 safe 和 intent 前恢复。
- packages/durable/test/harness-structured-output.test.ts:180–222：模型调用的结构化输出违约。
- packages/agent/test/agent-tool-typebox.test.ts:1–65：TypeBox 常见类型的静态兼容。

以上为静态阅读定位。本次没有执行 Pi 测试。

## 可观测性

已有 Langfuse skill 提供操作指南，没有安装 SDK 或配置服务的证据。
设计先固定 run/turn/toolCall 关联关系，以及延迟、调用数、用量和终止原因。
工具 I/O 和模型请求包含敏感数据，记录前脱敏，按主体隔离访问。

Langfuse 的具体 SDK 版本和 API 接入尚未设计。
接入时须按现有 skill 读取最新官方文档并核对版本。
可观测系统异常不能修改业务操作事实；关键审计收据留在权威存储。

## 证据缺口

- 模型访问：Python 模型 SDK、模型供应商及其流式/工具调用契约尚未确定。
- 产品范围：没有可确认的业务工具集合。
- 部署：单实例、多实例、任务期限和存储均未确定。
- 权限：身份系统、租户划分和审批来源未确定。
- 写接口：是否支持业务幂等键、状态查询和补偿未验证。
- 性能：没有延迟、吞吐、token 费用或恢复耗时实测。
- 依赖：发布包与本地 checkout 的完整契约一致性未验证。

## 下一项工程动作

形成一份领域工具清单，为每项标注输入、输出、资源权限、读写性质、审批和幂等能力。
在现有 Python/FastAPI 分层中确认模型 SDK、权威存储及单写者部署约束。
输入和结果验证使用成熟验证库，按项目声明直接依赖并锁定可复现版本。
以本目录契约和验证集作为实现准入条件。
