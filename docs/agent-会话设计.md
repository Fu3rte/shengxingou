# Agent 会话设计

## 存储结构

每个会话保存在 `backend/.data/sessions/<session_id>.jsonl`。首行为会话头，其余记录按写入顺序追加。

| 类型 | 字段 | 说明 |
| --- | --- | --- |
| `session` | `version`、`id`、`created_at` | 格式版本、会话 ID、UTC 创建时间 |
| `message` | `id`、`timestamp`、`run_id`、`message` | 保存一条 `AgentMessage` |
| `session_info` | `id`、`timestamp`、`title` | 保存会话名称变更 |
| `event` | `id`、`timestamp`、`event` | 保存一条执行事件 |

`version` 初始为 `1`。记录 ID 在会话内唯一。`message.run_id` 标识所属运行；不属于某次运行的系统消息使用 `null`。会话名称取最后写入的 `session_info.title`，可以为空。

```jsonl
{"type":"session","version":1,"id":"s1","created_at":"2026-01-01T00:00:00Z"}
{"type":"message","id":"n1","timestamp":"2026-01-01T00:00:00Z","run_id":null,"message":{"role":"system","content":"你是助手"}}
{"type":"event","id":"n2","timestamp":"2026-01-01T00:00:01Z","event":{"run_id":"r1","seq":1,"time":"2026-01-01T00:00:01Z","type":"run.started","data":{"input":"你好"}}}
{"type":"message","id":"n3","timestamp":"2026-01-01T00:00:01Z","run_id":"r1","message":{"role":"user","content":"你好"}}
```

## 窗口与会话

新窗口默认创建独立 Session；刷新保留当前 Session。用户可以通过会话列表打开历史 Session。不同 Session 的消息、运行状态和上下文相互隔离。

系统为本机单用户使用，不设计登录、注册和账号体系。待办数据按 Session 隔离，同一 Session 的各次 Run 共享；待办成功操作的 `tool.completed` 事件是唯一持久记录。重新打开会话或重启服务时，按会话 JSONL 中的全部待办成功事件重建清单和递增 ID，不重新执行工具。已记录的操作在所属 Run 停止、失败或中断后仍保留。

## 上下文构建

`build_session_context(active_run_id)` 按记录顺序提取所属 Session 的消息，返回线性的 `AgentMessage` 数组。模型适配层过滤自定义消息并转换为供应商格式。

- 历史上下文包含不属于 Run 的系统消息及 `completed` Run 的消息；已压缩的历史使用对应摘要。当前运行由 `active_run_id` 标识，其消息参与本次上下文。
- 重试在会话末尾创建新 Run，历史上下文仍只包含已完成 Run 的消息。
- 发送模型请求前校验工具调用与结果的 ID 配对；不完整的工具调用不能发送。
- 同一会话同时执行一个运行；不同会话可以并发运行。

## 上下文压缩

当前 Session 与事件阶段不实现上下文压缩及摘要。以下保留已确定的后续设计。

输入上下文预算上限为 20000 token，估算达到 18000 token 时自动压缩。系统指令、工具声明、历史摘要、近期消息和当前 Run 消息均参与输入预算统计；模型输出占用模型窗口的额外空间。

使用与模型匹配的本地 tokenizer 估算 token；受支持的 OpenAI 模型使用 `tiktoken`。未知模型须显式配置兼容的计数方式，无法计数时直接报错。供应商对请求封装的计数可能存在差异，实际模型窗口也可能小于输入预算与输出所需空间。

使用当前模型摘要较早的已完成 Run，保留最近两个完整 Run 和当前 Run；工具调用及其结果保持配对。摘要请求不提供工具，计入当前 Run 的模型请求次数。摘要包含用户目标、偏好、重要约束、工具结论和未解决问题；待办状态仍以已持久化的待办事件为准。

摘要、覆盖范围及压缩过程的模型请求和响应写入执行记录；摘要持久化后生效，即使触发压缩的 Run 随后失败或停止仍可使用。原始消息和事件继续保留。压缩失败或无法满足预算时，当前 Run 报错，已保存记录保持可用。

## 执行事件

`event` 包含 `run_id`、从 1 开始递增的 `seq`、UTC `time`、`type` 和 `data`。事件保存在会话 JSONL 中，不进入模型上下文。

事件包括 `run.started`、`model.request`、`model.delta`、`model.response`、`tool.started`、`tool.completed`、`tool.confirmation_requested`、`tool.confirmation_resolved`、`steer.queued`、`steer.applied`、`run.completed` 和 `run.finished`。上下文压缩记录属于后续设计。模型与工具事件的 `data` 包含 `turn_index`。异常、停止、超限或中断时，`run.finished` 记录对应状态及可用的错误信息。

通过密钥检查的模型请求、归一化响应片段和完整响应、工具参数及结果均需记录。供应商原始 chunk 和未知扩展不写入记录或推送；归一化事件持久化后用于 SSE 补发。异常日志关联 `run_id`，异常继续终止执行。

## 事件传输

- 每个 Run 使用独立 SSE 连接。请求指定 `session_id` 和 `run_id`，服务端校验 Run 属于该 Session。
- 前端先通过 HTTP 获取该 Run 的事件快照和最后一条事件的 `seq`，再从该游标之后接收 SSE。快照为空时从第一条事件开始；快照已包含终止事件时，无需建立连接。
- SSE 的 `id` 使用事件 `seq`，`data` 包含完整执行事件。事件写入 JSONL 后才可发送。
- 断线重连使用 `Last-Event-ID` 补发；该请求头优先于首次连接的快照游标。`Last-Event-ID` 表示浏览器收到的最后事件 ID；页面刷新时重新读取快照。
- 服务端按 JSONL 写入顺序补发，回放与实时接收的衔接不能遗漏事件。前端在所属 Session 内按 `(run_id, seq)` 去重。
- 客户端收到 `run.completed` 或 `run.finished` 后关闭该 Run 的 SSE 连接。
- 服务重启后，对缺少终止事件的 Run 追加状态为 `interrupted` 的 `run.finished`。事件时间表示恢复时的检测时间；已存在终止事件的 Run 不重复追加。
- 无效游标或损坏的 JSONL 明确报错。JSONL 存在残缺行时，停止读取，不自动修复或追加终止事件。
- SSE 请求遵守本机访问与 Origin 限制。代理需流式转发事件并透传 `Last-Event-ID`。

## 写入与安全

- 每个 Session 采用单写入者并追加记录。重复 ID 和尾部残缺行视为损坏，停止读取。
- 会话数据可能包含提示词、工具参数和结果；API key 和环境变量不写入记录。
- 本机服务仅允许本机访问；会话和日志无自动过期。
