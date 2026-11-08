# Agent 会话设计

## 代码边界

- `backend/src/agent/session_records.py`：记录类型、JSONL 解析和历史事件校验。
- `backend/src/agent/session_stream.py`：模型流归一化、事件序号和文件变化检测，通过 Session 的公开读写接口保存记录。
- `backend/src/agent/session.py`：Session、会话管理函数、文件读写、锁、工具执行协调、上下文和恢复。

## 存储结构

每个会话保存在 `backend/.data/sessions/<session_id>.jsonl`。首行为会话头，其余记录按写入顺序追加。

| 类型 | 字段 | 说明 |
| --- | --- | --- |
| `session` | `version`、`id`、`created_at` | 格式版本、会话 ID、UTC 创建时间 |
| `message` | `id`、`timestamp`、`run_id`、`message`、可选 `queue_id` | 保存一条 `AgentMessage`，`queue_id` 标识 steering 消息 |
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

系统为本机单用户使用，不设计登录、注册和账号体系。每个 Session 使用 `backend/.data/sessions/.todo/session-<session_id>.jsonl` 保存独立待办清单和递增 ID；待办工具事件保存在所属 Session 的 JSONL 中。重新打开会话时由待办模块校验历史待办结果；待办文件由待办模块读取和写入，Session 协调锁与事件时序。重新打开会话或重启服务时读取对应的待办文件。

## 上下文构建

`build_session_context(active_run_id)` 按记录顺序提取所属 Session 的消息，返回线性的 `AgentMessage` 数组。模型适配层过滤自定义消息并转换为供应商格式。

- 历史上下文包含不属于 Run 的系统消息及 `completed` Run 的消息；已压缩的历史使用对应摘要。当前运行由 `active_run_id` 标识，其消息参与本次上下文。
- 带 `queue_id` 的 steering 消息须有对应的 `steer.applied` 事件才进入上下文；未提交消息保留在记录中。
- 重试在会话末尾创建新 Run，历史上下文仍只包含已完成 Run 的消息。
- 发送模型请求前校验工具调用与结果的 ID 配对；不完整的工具调用不能发送。
- 同一会话同时执行一个运行；不同会话可以并发运行。

## 执行事件

`event` 包含 `run_id`、从 1 开始递增的 `seq`、UTC `time`、`type` 和 `data`。事件保存在会话 JSONL 中，不进入模型上下文。

事件包括 `run.started`、`model.request`、`model.delta`、`model.response`、`compact.request`、`compact.completed`、`tool.started`、`tool.completed`、`tool.confirmation_requested`、`tool.confirmation_resolved`、`steer.queued`、`steer.applied`、`run.completed` 和 `run.finished`。压缩记录保存结构化摘要、覆盖的消息 ID 和前一压缩记录 ID；摘要请求单独记录，不作为业务回答流输出。模型与工具事件的 `data` 包含 `turn_index`。异常、停止、超限或中断时，`run.finished` 记录对应状态及可用的错误信息。

通过密钥检查的模型请求、归一化响应片段、可用的响应信息、工具参数及结果均需记录。供应商原始 chunk 和未知扩展不写入记录或推送；归一化事件持久化后用于 SSE 补发。模型事件字段见 `agent-消息转换设计.md`；steering 和终态规则见 `agent-loop-设计.md`。异常日志关联 `run_id`，异常继续终止执行。

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
- 文件打开后写入、刷新或关闭失败时，当前进程拒绝继续读取该会话；重启后按 JSONL 损坏规则检查。
- 本机服务仅允许本机访问；会话和日志无自动过期。
