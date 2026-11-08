# Agent 消息与转换设计

## 消息结构

`Message` 包含四种角色：`system`、`user`、`assistant`、`toolResult`。`AgentMessage` 包含 `Message` 和运行时自定义消息。内部消息和内容块使用 Pydantic 模型及类型联合，写入 JSONL 时转换为 JSON。

- `system.content` 和 `user.content` 为字符串。
- `assistant.content` 为有序内容块数组，支持 `text` 和 `toolCall`。
- `toolCall` 包含字符串 `id`、`name` 和 `arguments`。`arguments` 保留模型返回的原始 JSON 字符串。
- `toolResult` 包含字符串 `tool_call_id` 和 `content`，通过 `tool_call_id` 关联工具调用。
- 自定义消息包含 `role: "custom"`、`type` 和 JSON 格式的 `data`，默认不进入模型上下文。

```jsonl
{"role":"system","content":"需要时使用计算器"}
{"role":"user","content":"1+2"}
{"role":"assistant","content":[{"type":"toolCall","id":"call_1","name":"calculate","arguments":"{\"left\":\"1\",\"operator\":\"+\",\"right\":\"2\"}"}]}
{"role":"toolResult","tool_call_id":"call_1","content":"{\"result\":\"3\"}"}
{"role":"assistant","content":[{"type":"text","text":"3"}]}
```

## 模型适配

OpenAI 适配模块负责请求和响应两个方向：

- 请求转换：筛选模型可见的 `Message`，转换为 Chat Completions 消息。内部 `toolCall` 转为 `tool_calls`，`toolResult` 转为 `tool` 消息。
- assistant 文本块按顺序直接拼接；仅有工具调用时，OpenAI 请求的 `content` 为 `null`。请求使用 `n=1`。
- OpenAI 流式响应由适配器读取并归一化为内部片段；安全检查通过后按顺序记录和推送。流结束后，将完整响应转换为 assistant 消息、`finish_reason` 和可用用量。
- 部分 chunk 的 choice 列表可以为空，usage 也可以缺失；聚合后的响应应包含一个 choice。支持的用量信息按供应商实际返回记录，缺失时不伪造。
- 转换保留工具调用 ID、名称、顺序和原始参数。参数在完整工具调用生成后、执行前校验。
- `refusal` 转为 assistant 的 `text` 块。`content` 与 `refusal` 同时存在时，按此顺序生成文本块。
- `refusal` 与工具调用同时出现时，终止 Run，不执行工具。`length`、`content_filter` 等不完整响应终止 Run。
- choice 数量异常、未支持的角色或工具调用类型、响应与 `finish_reason` 不一致时直接报错。
- `convert_to_llm` 的筛选职责可以合并到请求转换函数中。

Chat Completions 分开表示文本和工具调用，跨字段的交错顺序无法从该接口响应中恢复。归一化格式只包含已支持的消息字段和执行信息；未知供应商扩展不进入会话、Trace 或 SSE。供应商要求回传扩展字段时，须确认并适配该供应商协议。

### 响应字段映射

| OpenAI Chat Completions 字段 | 内部表示 |
| --- | --- |
| `choices[0].message.content` | assistant 的 `text` 内容块 |
| `choices[0].message.refusal` | assistant 的 `text` 内容块 |
| `choices[0].message.tool_calls[].id` | `toolCall.id` |
| `choices[0].message.tool_calls[].function.name` | `toolCall.name` |
| `choices[0].message.tool_calls[].function.arguments` | `toolCall.arguments`，保留 JSON 字符串 |
| `choices[0].finish_reason` | 响应结束原因，用于校验与运行记录 |
| `usage` | 可用的用量信息，用于运行记录 |

例如，一个包含文本与工具调用的 assistant 响应归一化为：

```json
{"role":"assistant","content":[{"type":"text","text":"我来计算"},{"type":"toolCall","id":"call_1","name":"calculate","arguments":"{\"left\":\"1\",\"operator\":\"+\",\"right\":\"2\"}"}]}
```

### 当前代码状态

- `backend/src/agent/types.py` 已定义 `AgentMessage`、`TextContent` 和 `ToolCallContent`。
- `backend/src/ai/models/openai.py` 的 `to_openai_messages` 与 `from_openai_response` 已实现消息双向转换。完成响应中的 `content`、`refusal` 和 `tool_calls` 可转换为内部内容块。
- `async_convert_stream` 逐个交付 SDK 模型序列化后的 chunk，最终 `ModelResponse` 仍包含 SDK 响应对象。`backend/src/ai/models/normalized.py` 提供异步归一化流事件。SDK 序列化结果不保证等于供应商的 HTTP 原始字节。
- `backend/src/ai/models/openai_stream.py` 接收异步 SDK chunk，并通过 `backend/src/agent/session_stream.py` 的 `record_model_stream_async` 完成安全归一化和持久化。
- `model.response` 记录完整消息或失败时可用的结束原因和用量。
- `content` 和 `refusal` 片段交错时，归一化文本事件按最终消息的文本顺序输出。

## 会话与执行记录

每个会话的消息和执行事件按顺序保存在同一个 JSONL 文件。模型上下文从会话消息重建。

执行记录独立于 `AgentMessage`，每条包含 `run_id`、递增的 `seq`、UTC `time`、`type` 和 `data`：

| 类型 | 数据 |
| --- | --- |
| `run.started` | 本次输入 |
| `model.request` | 模型名、请求消息和工具定义 |
| `model.delta` | 通过安全检查的归一化文本或工具调用片段 |
| `model.response` | 可用的归一化响应内容、结束原因和用量信息 |
| `compact.request`、`compact.completed` | 摘要请求及结构化摘要、覆盖的消息 ID 和前一压缩记录 ID |
| `tool.started` | 工具调用 ID、名称和原始参数 |
| `tool.completed` | 工具调用 ID、名称、原始参数和结果 |
| `tool.confirmation_requested` | Turn 序号、工具调用 ID、名称、操作和待办任务信息 |
| `tool.confirmation_resolved` | Turn 序号、工具调用 ID 和用户选择 |
| `steer.queued` | `queue_id`、`content` |
| `steer.applied` | `queue_id`、`message_id` |
| `run.completed` | 最终回答 |
| `run.finished` | 异常、停止、超限或中断时的状态及可用的错误信息 |

### 模型流事件格式

模型与工具事件的 `data` 包含从 1 开始的 `turn_index`。`model.delta.data` 包含 `turn_index` 和非空 `fragments` 对象：

```json
{"turn_index":1,"fragments":{"text":"你好"}}
```

`fragments` 的值均为字符串；允许的键为 `text`、`tool:<index>:id`、`tool:<index>:name` 和 `tool:<index>:arguments`。工具片段使用工具调用索引关联。文本事件在成功响应中拼接后的顺序须与最终 assistant 消息中的文本块一致；`content` 和 `refusal` 同时存在时，按最终消息的顺序处理。

`model.response.data` 包含 `turn_index`、`message`、`finish_reason` 和 `usage`。收到模型响应且响应不完整时，记录可用信息；没有完整消息时 `message` 为 `null`，结束原因或用量不可用时相应字段为 `null`。例如：

```json
{"turn_index":1,"message":null,"finish_reason":"length","usage":null}
```

响应不完整或校验失败时终止 Run，不将其追加为完整 assistant 消息或执行其中的工具。流中断时保留已写入的安全事件。

归一化事件写入后才可通过 SSE 推送，供断线补发；供应商原始 chunk 和未知扩展不写入记录或推送。`model.response` 不直接序列化完整 SDK 响应对象。

对需要写入或推送的文本和工具参数片段进行跨片段密钥检查；暂存尚未确认安全的片段，并保持事件顺序。工具参数及其后的片段等待完整响应，解析参数 JSON 并检查解码内容后才写入或推送；响应失败时未确认的参数片段不写入。模型请求、完整响应及工具内容也须在对应边界检查。

用户输入、模型请求、工具参数、工具结果和归一化模型输出在写入记录或发送前检查已配置密钥的明文；工具参数还须在执行前检查。检测到密钥时拒绝相关内容并终止 Run，错误记录不包含密钥；不记录认证头和环境变量。明文匹配无法识别未知或变形的秘密，也不能保证识别拆分在不同逻辑字段或工具调用中的秘密。本机服务仅允许本机访问，会话和日志无自动过期。
