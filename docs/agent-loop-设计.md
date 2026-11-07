# Agent Loop 设计

## 运行粒度

- `Session`：一次会话，包含多次 Run。
- `Run`：从用户消息触发执行，到最终回答、停止、超限或失败的一次运行。
- `Turn`：一次模型调用及该次响应触发的全部工具执行。没有工具调用时，Turn 只包含模型调用。
- `Trace`：一个 Run 的执行记录视图。一个 Session 包含多条 Trace，一条 Trace 包含该 Run 的零个或多个 Turn。

例如：Turn 1 调用模型并执行两个工具；Turn 2 再次调用模型并返回最终回答。这两个 Turn 属于同一个 Run 和 Trace。

`Run` 包含 `run_id`、`session_id`、状态、开始时间和结束时间；运行中结束时间为空。`Turn` 使用 `run_id` 与从 1 开始的 `turn_index` 标识。Trace 从同一 `run_id` 的事件重建，无需重复保存消息和事件。接入 Langfuse 时再建立 `trace_id` 映射。

每个 Session 同时最多执行一个 Run；不同 Session 可以并发执行。各 Session 的上下文、steering 队列和停止信号相互隔离。

## 服务调用接口

`backend/src/agent/agent_loop.py` 提供 `AgentLoop(session, client, model)`，其中 `client` 为使用固定 API key 的 `AsyncOpenAI`。客户端仅用于 AgentLoop 的模型事件循环，使用固定凭据和标准 HTTP transport；不支持动态密钥、HTTP event hooks、URL 身份信息、默认查询参数、Cookie 或代理认证。Loop 为客户端禁用 Cookie 的接收与发送；调用期间不得更改凭据。`append_message(content)` 返回所属 `run_id`；`trace(run_id)` 读取事件快照；`events(run_id, after)` 按序交付已持久化事件；`abort(run_id)` 提交停止信号；`confirm(run_id, turn_index, tool_call_id, decision)` 提交删除选择；`wait(run_id, timeout)` 等待执行并传播错误。`close(timeout)` 等待当前操作退出，未结束的 Run 在重新打开时恢复为 `interrupted`。调用方在所有 Run 退出后使用 `close_model_client(client)` 关闭客户端。

这些同步接口不得在模型事件循环线程中调用。其他进程持有同一 Session 的运行锁时，创建或恢复运行直接报锁冲突。接口已通过 HTTP、SSE 接入前端。

## 消息追加

- 空闲时，`append_message` 创建 Run，追加用户消息并启动循环。
- 运行时，`append_message` 将用户消息加入 steering 队列，并写入 `steer.queued` 事件。
- 当前模型响应和工具批次结束后，按入队顺序追加 steering 消息，记录 `steer.applied`，继续请求模型。
- 当前工具批次按顺序执行；steering 不跳过批次中的工具。
- 运行停止或失败时，未消费的消息保留在事件记录中，不自动注入后续 Run。
- 入队、最终空队列检查和结束运行须串行化，避免并发追加时丢失消息。

## 循环

1. 创建 Run，记录 `run.started`，追加用户消息。
2. 根据当前 Session 的顺序记录构建上下文，检查停止信号和模型请求次数上限。
3. 记录模型请求并发起流式调用，将响应片段归一化；安全检查通过后按顺序记录和推送内部事件。流结束后记录归一化响应，校验通过后追加 assistant 消息。
4. 响应包含工具调用时，按顺序查找工具并校验参数，执行工具，记录执行事件并追加 `toolResult`。
5. 当前工具批次完成后，追加已接收的 steering 消息。
6. 有工具结果需要模型处理或有已追加的 steering 消息时，继续下一次模型调用。
7. 模型返回完整最终回答且没有待处理 steering 时，记录 `run.completed` 并结束。

待办删除执行前暂停当前 Run，展示任务 ID 和内容并等待用户确认。确认后继续执行；用户拒绝或停止时，删除不执行，Run 以 `stopped` 结束。服务重启后，等待确认的 Run 按既定规则标记为 `interrupted`。

每个 Run 最多发起 64 次模型请求；后续实现压缩时，摘要请求计入其中。模型响应不完整、工具错误或其他异常直接终止，并记录对应状态。需要继续请求且已达到上限时，Run 以 `limited` 状态结束。

## 协作式停止

`abort` 设置停止信号。信号生效后，不再开始新的模型请求或工具。已开始的同步请求或工具可以继续到返回，已产生的外部副作用保留。

模型流进行中收到停止信号时，继续接收并记录当前响应；响应结束后不执行新的工具或模型请求。工具执行完成后记录结果，随后停止。进程重启后，对缺少终止事件的运行追加状态为 `interrupted` 的 `run.finished`。

停止信号、工具启动、最终结束检查和终态提交须在同一 Session 内串行化：

- 停止在终态持久化前生效时，当前操作正常结束并记录后，Run 以 `stopped` 结束。
- 终态提交前发生模型、工具或安全错误时，Run 以 `failed` 结束；错误信息须通过安全检查。
- `run.completed` 已持久化后，停止请求返回已结束，不能覆盖终态。
- 写入失败导致终态无法持久化时，向调用方报告失败，不生成虚假的终态记录。

## 执行事件

事件与消息按顺序保存在同一个 Session JSONL 文件中。模型和工具事件的 `data` 包含 `turn_index`。

| 类型 | 数据 |
| --- | --- |
| `run.started` | 本次输入 |
| `model.request` | 模型请求 |
| `model.delta` | 通过安全检查的归一化文本或工具调用片段 |
| `model.response` | 可用的归一化响应内容、结束原因和用量信息 |
| 上下文压缩记录（后续） | 摘要及其覆盖的历史范围 |
| `tool.started`、`tool.completed` | 工具调用 ID、名称、参数和结果 |
| `tool.confirmation_requested` | Turn 序号、工具调用 ID、名称、操作和待办任务信息 |
| `tool.confirmation_resolved` | Turn 序号、工具调用 ID 和用户选择 |
| `steer.queued` | `queue_id`、`content` |
| `steer.applied` | `queue_id`、`message_id` |
| `run.completed` | 最终回答 |
| `run.finished` | 停止、超限、失败或中断的状态及可用的错误信息 |

`steer.queued.data` 中的 `queue_id` 标识排队消息，`content` 是消息文本：

```json
{"queue_id":"q1","content":"换一种方式"}
```

`steer.applied.data` 中的 `queue_id` 关联该消息，`message_id` 标识追加到 Session 的消息记录：

```json
{"queue_id":"q1","message_id":"m1"}
```

`model.delta` 和 `model.response` 的数据格式见 `agent-消息转换设计.md`。

供应商原始 chunk 和未知扩展不写入记录或推送。执行事件通过 SSE 推送至前端。本机服务仅允许本机访问；会话和日志无自动过期。执行记录可能包含用户内容、提示词和工具数据；API key 和环境变量不写入记录。
