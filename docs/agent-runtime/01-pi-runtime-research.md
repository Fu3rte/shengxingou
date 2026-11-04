# Pi Agent Loop 与 Tool 调研

## 当前证据基线

本地源码：D:/Repository/pi。
固定 HEAD：b64e6badd6ddefda17fc7f6286c013467186d8a1。
提交时间：2026-11-04T09:18:01+01:00。
agent-core、ai、durable、schema 的 package.json 均标记 1.5.0。
HEAD 含 Unreleased 修改，例如 schema 相对 $id/$ref 修复。
本地源码与 npm 发布包需要独立核对。

[固定 commit](https://github.com/earendil-works/pi/commit/b64e6badd6ddefda17fc7f6286c013467186d8a1)。
以下 Pi 文件路径均相对 D:/Repository/pi/；行号限定于该 commit。

三名 fresh-context 子代理分别复核 loop、tool、官方文档与选型。
主代理直接核查关键实现、限定路径 diff 和项目当前配置。
本轮使用本地官方源码、README、CHANGELOG 和测试作为证据，没有联网或运行行为测试。

与 42a3497d03ad17e308a2299fa824727894f2c0ec 的限定路径比较：
agent-core 主循环调度没有实质变化，新增 provider attribution；工具校验已迁移到 pi-schema。
durable generation、收尾与存储提交机制有较多变化，需按当前契约研究。

## 项目当前约束

backend/pyproject.toml:1–9 声明 Python >=3.14、FastAPI、Uvicorn。
backend/src/ 存在 application、domain、infrastructure、interfaces 分层目录。
本次限定发现中，src/ 和 tests/ 没有 Python 文件；发现工具遵循忽略规则。
frontend/package.json 声明 React、TypeScript 和 Vite，test 脚本尚无可执行测试。
AGENTS.md:1–3 提供 Pi 路径并规定前端不得添加小字提示。

由此，设计面向现有 Python/FastAPI 后端。
Pi 的 TypeScript 包作为架构和行为参考；直接嵌入适用于 TypeScript 宿主。
本项目没有采用 Pi 包、模型 SDK、业务工具或存储的代码证据。

推荐保持模型接入、loop、业务 executor、权威存储的责任边界。
Python 模型调用和输入验证使用成熟 SDK/验证库，具体版本在实现准入时核对。
项目接入位置可沿现有 application/domain/infrastructure/interfaces 分层确定。

## 包职责

- pi-ai：统一模型接口、消息、流式响应和工具参数验证。
- pi-agent-core：进程内 Agent、loop、工具执行和事件。
- pi-coding-agent：编码应用、资源加载、会话、扩展及工具暴露策略。
- pi-durable：独立的持久化 Session、task、document 和 Harness，直接基于 pi-ai 与 Chord。
- pi-schema：JSON Schema builder、类型推导和无 eval 的校验器。

provider identity/attribution 表示请求来源归因，没有业务用户认证效力。
工具可见性、hook 和重放标注均不能替代主体、租户与资源权限。

证据：packages/agent/package.json:1–29；packages/schema/README.md:1–13；
packages/agent/src/agent-loop.ts:34–42,412–418；packages/durable/README.md:1–7,92–104。

## Agent Loop

### 调度与终止

内层处理模型、工具及 steering；外层在自然收尾检查 follow-up。
一次 turn 是模型响应及其工具批次。
完整 assistant 响应后，error/aborted 调用 finishTurn 后硬退出。
正常响应先完成工具结果，再调用 finishTurn，之后发 turn_end。

finishTurn=end 阻止后继请求和队列消费。
finishTurn=continue 保证一个后继请求；工具、steering 或 follow-up 已触发的请求满足该决定。
没有自然后继时，会产生一次 context-only 请求；供应商兼容性尚未实测。

length 截断消息的所有工具调用均禁止执行，并生成错误结果。
Pi 默认仍可继续下一次请求；项目的 limited 终止需显式实现。
工具错误同样默认变成 toolResult，不自动形成应用的 fail-fast。

证据：packages/agent/src/agent-loop.ts:170–325,478–510；
packages/agent/test/agent-loop.test.ts:456–588,1108–1405。

### 队列与运行归属

steering 在开始及整批工具完成后读取。
prepareNextTurn 期间新到达的 steering 可在先前没有待处理输入时再次读取。
prepareRequest 之后不再重新轮询。
follow-up 在同一次 Pi loop/run 内消费，两个内存队列没有容量或持久化保证。

Agent.continue 在 assistant 尾部可优先消费排队 steering，再消费 follow-up；无可用输入时拒绝。
低层 continue 直接拒绝 assistant 尾部。
本项目的持久 inbox 和后续输入新 run 语义需由宿主独立实现。

证据：packages/agent/src/agent.ts:145–174,298–350,379–405；
packages/agent/src/agent-loop.ts:175–229,299–319。

### 流式、屏障和取消

transformContext 先于 convertToLlm；部分工具参数只用于展示。
Agent 先更新内存状态，再按注册顺序 await 订阅者。
完整 assistant 的 message_end 监听结算后才开始工具。
agent_end 监听结算后，实例才 idle。

Agent.subscribe 和 runAgentLoop 的 awaited sink 可承载持久化屏障。
低层 agentLoop 的 EventStream 消费者不提供同样保证。
关键监听者失败可能触发 Agent 合成失败事件，需防止重复进入失败写入路径。

abort 只传递 signal，工具及底层 I/O 必须配合。
单实例运行互斥无法提供跨进程准入或副作用撤销。
预算需在每次请求和每次工具前检查，finishTurn 的时机无法阻止同批后续工具。
未知/无效调用不进入 beforeToolCall，调用预算须从完整 assistant 清单计数。

证据：packages/agent/src/agent.ts:512–638；
packages/agent/src/agent-loop.ts:381–476,708–785,826–866。

## Tool

### Schema 与原始输入

AgentTool.parameters 接受 JSON Schema object；Static 来自 pi-schema。
常用 TypeBox 1.x JSON Schema 仍有类型兼容，部分类型推导为 unknown。
JavaScript 专用类型、refinement 和 codec 不在支持范围。

validateToolArguments 克隆参数，以 formats、coerce=true、unresolvedRefs=reject 编译后 parse。
转换可能改变业务值：整数输入字符串“3.5”可变成 3，必填字符串 null 可变成“null”。
optional null 也可能被删除。
应用严格输入策略必须检查转换前的原始参数。
审批、幂等键和参数摘要需绑定明确的最终执行值。
校验错误含 Received arguments，外发前必须脱敏。

证据：packages/schema/README.md:20–155；packages/ai/src/utils/validation.ts:1–68；
packages/agent/src/types.ts:466–510。

[参数验证实现](https://github.com/earendil-works/pi/blob/b64e6badd6ddefda17fc7f6286c013467186d8a1/packages/ai/src/utils/validation.ts#L1-L68)。

### 执行管线与权限

agent-core 依次查名称、prepareArguments、校验、beforeToolCall、execute、afterToolCall。
before hook 可原地改变已校验参数，之后不二次校验。
真实测试明确断言被改成数字的字符串参数进入 execute。
durable 的 beforeTool 后会再次验证最终参数。

coding-agent 支持同名工具覆盖；codemode/deferred 工具有内部调用面。
可见性和作者声明的 readOnly/idempotent 标注不构成业务授权。
项目的唯一工具名、固定版本、统一权限入口均为应用约束。

证据：packages/agent/src/agent-loop.ts:707–809；
packages/agent/test/agent-loop.test.ts:528–588；
packages/durable/src/harness/tool.ts:130–226；
packages/coding-agent/src/core/agent-session.ts:1550–1634,3576–3673。

### 结果、错误和并发

agent-core 的 content 给模型，details 给 UI/日志，structuredContent 面向程序。
outputSchema 在所读执行路径中没有自动运行时校验。
afterToolCall 还能替换结果；最终验证与全通道脱敏需要应用负责。

默认 parallel；一项 sequential 会使整批串行。
预检顺序执行，通过 Promise.all 并行启动，没有批内并发上限。
结束事件按完成顺序；toolResult 消息按原始调用顺序。
terminate 要求所有已结算结果一致；单项错误不立即终止整批。
execute 抛错会被转换成 isError，工具超时和真实 I/O 取消由执行体负责。

证据：packages/agent/src/types.ts:420–510；
packages/agent/src/agent-loop.ts:518–700,826–920；
packages/agent/test/agent-loop.test.ts:668–761,867–1107,1732–1963。

## Durable 的不同契约

### 持久任务与收尾

generation checkpoint 包含 prepare/request/retry/poll/tools/finish/drain。
支持中断请求重试、deferred poll 恢复和 afterRun 子任务。
答案已提交或输入 settled，不等于 generation 的收尾工作全部结束。
maxTurns 按工具轮计数，onYield continuation 可重置该计数。
它不等价于应用的所有模型请求硬预算。

证据：packages/durable/src/harness/generation.ts:71–145,265–405,857–917,1006–1139。

### Intent 与重放

工具执行前提交最终参数和 safe/unsafe 策略，默认 unsafe。
恢复时，记录策略和当前工具策略均 safe 才重跑。
safe 恢复直接执行已存参数，不重跑 beforeTool 或参数验证。
其余调用生成 interrupted；外部业务 operationId、当前权限与幂等查询仍由应用提供。
agent-core 的对应禁止重放字面值是 never，需要明确映射。

证据：packages/durable/src/harness/tool.ts:153–226；
packages/durable/test/harness-tools-recovery.test.ts:109–310。

### 输出违规按调用来源处理

afterTool 后执行输出限制及 structuredOutputSchema 校验。
嵌套调用的非法结构化结果转为 isError 和 invalid_structured_output。
模型调用则可仅丢弃该字段并报告宿主，模型结果仍可能成功。
项目统一 invalid_output 终止策略需另行实现。

证据：packages/durable/src/harness/tool.ts:669–803；
packages/durable/test/harness-structured-output.test.ts:180–222。

[最终结果处理](https://github.com/earendil-works/pi/blob/b64e6badd6ddefda17fc7f6286c013467186d8a1/packages/durable/src/harness/tool.ts#L669-L803)。

### Deferred progress 与存储

partial 和工具输出默认最多约每 100 ms 更新一次可见状态。
deferred commit 可以先发布到 watch/内存，最迟默认约 1,000 ms 后存储。
崩溃可丢失这段进度，首秒内中断的尝试还可能没有已记录 usage。

证据存在表述张力：README 开头概述为展示前存储，详细 spec 明确 deferred 例外。
本报告以 spec 的具体例外和 README 进度段落限定一致性保证。
三名研究者均未完成全存储实现审计，不能把每次 watch 回调当成持久化证明。

存储仍由单进程拥有，没有跨进程锁。
SQLite WAL/NORMAL 对宿主故障或断电的保证有限。
durable 仍标记 experimental，存储与 pi.live 版本升级须验证回滚能力。

证据：packages/durable/docs/spec.md:59–90；packages/durable/README.md:523–527,850–881；
packages/durable/CHANGELOG.md:5–11,49–58,120–153,191–200。

[一致性不变量](https://github.com/earendil-works/pi/blob/b64e6badd6ddefda17fc7f6286c013467186d8a1/packages/durable/docs/spec.md#L59-L90)。

## 推荐与证据强度

采用 Python 服务端边界，参考 Pi loop 的完整响应、工具批次、结果回填和终止机制。
以业务 executor 明确权限、严格验证、审批、deadline、结果约束与 intent/result 收据。
确定消息与业务结果先存储再发布；草稿允许丢失。
具体设计见 [Loop](02-agent-loop-design.md)、[Tool](03-tool-design.md)。

高置信度：loop 分支、schema 转换、hook 后校验差异、输出违规差异、safe 重放条件。
中置信度：整体接入成本、跨崩溃恢复与存储故障保证，缺少部署及故障注入实测。

本次未核验 npm 包、模型 SDK、生产身份系统、业务幂等协议、性能或断电恢复。
下一项工程输入是领域工具清单、模型供应商契约、写者模型及权威存储。
验证条件见 [验证与决策](04-validation-and-decisions.md)。
