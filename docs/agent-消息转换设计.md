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
- 使用 OpenAI SDK 的流式接口与聚合能力。原始 chunk 通过安全检查后按顺序记录；流结束后，将聚合响应转换为 assistant 消息与 `finish_reason`。
- 部分 chunk 的 choice 列表可以为空，usage 也可以缺失；聚合后的响应应包含一个 choice。用量信息按供应商实际返回记录。
- 转换保留工具调用 ID、名称、顺序和原始参数。参数在完整工具调用生成后、执行前校验。
- `refusal` 转为 assistant 的 `text` 块，原始字段保留在执行记录中。`content` 与 `refusal` 同时存在时，按此顺序生成文本块。
- `refusal` 与工具调用同时出现时，终止 Run，不执行工具。`length`、`content_filter` 等不完整响应终止 Run。
- choice 数量异常、未支持的角色或工具调用类型、响应与 `finish_reason` 不一致时直接报错。
- `convert_to_llm` 的筛选职责可以合并到请求转换函数中。

Chat Completions 分开表示文本和工具调用，跨字段的交错顺序无法从该接口响应中恢复。

## 会话与执行记录

每个会话的消息和执行事件按顺序保存在同一个 JSONL 文件。模型上下文从会话消息重建。

执行记录独立于 `AgentMessage`，每条包含 `run_id`、递增的 `seq`、UTC `time`、`type` 和 `data`：

| 类型 | 数据 |
| --- | --- |
| `run.started` | 本次输入 |
| `model.request` | 模型名、请求消息和工具定义 |
| `model.delta` | 通过安全检查的原始模型响应 chunk |
| `model.response` | 流结束后的聚合响应、结束原因和可用的用量信息 |
| 上下文压缩记录 | 摘要及其覆盖的历史范围 |
| `tool.started` | 工具调用 ID、名称和原始参数 |
| `tool.completed` | 工具调用 ID、名称、原始参数和结果 |
| 删除确认请求及用户选择 | 工具调用 ID、待办任务信息及确认结果 |
| `steer.queued` | 队列消息 ID 和消息内容 |
| `steer.applied` | 队列消息 ID 和追加后的消息 ID |
| `run.completed` | 最终回答 |
| `run.finished` | 异常、停止、超限或中断时的状态及可用的错误信息 |

模型与工具事件的 `data` 包含 `turn_index`。通过安全检查的原始 chunk 写入 `model.delta.data.chunk`，按接收顺序记录并通过 SSE 推送；跨 chunk 检查需要暂存尚未确认安全的片段。聚合成功后记录 `model.response`；流中断时保留已写入的 chunk，终止 Run。

用户输入、模型请求、工具参数、工具结果和模型输出在写入记录或发送前检查已配置密钥的明文；工具参数还须在执行前检查。检测到密钥时拒绝相关内容并终止 Run，错误记录不包含密钥；不记录认证头和环境变量。明文匹配无法识别未知或变形的秘密。本机服务仅允许本机访问，会话和日志无自动过期。
