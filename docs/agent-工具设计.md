# Agent 工具设计

## 工具定义

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `str` | 工具名称，用于模型调用和注册表查找 |
| `description` | `str` | 提供给模型的工具说明 |
| `parameters` | `type[BaseModel]` | Pydantic 参数模型 |
| `execute` | 同步函数 | 执行工具并返回字符串 |

工具注册表使用 `name` 到工具定义的映射。Agent Loop 提供计算器、天气、搜索、文件读取、文件写入和待办工具；待办操作通过共享待办文件和 Session 事件执行。

注册时检查名称唯一性；重复名称直接报错。天气和搜索使用可自定义的 mock 数据，工具描述及结果标明数据来源和非实时性质。

模型所需的 function 声明由工具定义生成：`name`、`description` 和 `parameters.model_json_schema()`。参数在执行前使用同一个 Pydantic 模型校验；工具可以执行额外的业务规则校验。

文件读取与写入工具支持 UTF-8 文本和 Base64 编码的二进制内容；写入支持追加和覆盖。工具可以访问进程有权限访问的文件。普通工具接收校验后的参数模型。运行中的待办工具通过注册表调用所属 Session 的执行器，传入运行标识、调用 ID 和原始参数；Session 协调运行事件与锁顺序，待办模块负责共享文件、命令执行和删除确认快照校验。

所有 Session 共享 `backend/.data/sessions/.todo/todo.jsonl`，文件记录清单和递增 ID；各 Session 的 JSONL 分别记录工具事件。重新打开会话或重启服务时直接读取共享待办文件。删除确认后执行时重新检查任务 ID 和标题。

待办执行依次获取共享待办锁、会话工具锁和会话锁；结果通过密钥检查后写入共享文件，再记录 `tool.completed`。历史待办结果由待办模块按命令校验；事件持久化后才可推送成功事件或追加 `toolResult`。共享文件和会话事件分开写入，故障时二者可能不一致。JSONL 损坏时停止读取。用户手动重复提交请求可能重复添加待办。

## 调用流程

1. 循环检查停止信号，记录 `tool.started`，包含调用 ID、名称和原始参数。
2. 根据 `name` 在注册表中查找工具。
3. 使用 `parameters.model_validate_json(arguments)` 校验原始 JSON 参数。
4. 待办删除操作执行前暂停 Run，展示任务 ID 和内容，等待用户确认。
5. 通过注册表执行同步工具，取得字符串结果。
6. 记录 `tool.completed`，并追加包含调用 ID 和结果的 `toolResult`。

多个工具按顺序执行。调用 ID、停止信号和执行事件由运行过程管理。

## 删除确认事件

确认请求事件的 `type` 为 `tool.confirmation_requested`，`data` 字段全部必填：

```json
{"type":"tool.confirmation_requested","data":{"turn_index":1,"tool_call_id":"call_1","name":"todo","action":"delete","task":{"id":1,"title":"买牛奶"}}}
```

`turn_index` 是当前 Turn 序号，`tool_call_id` 是模型返回的工具调用 ID；`name` 固定为 `todo`，`action` 固定为 `delete`。`task` 包含待删除任务的正整数 `id` 和 `title`。

用户选择事件的 `type` 为 `tool.confirmation_resolved`，`data` 字段全部必填：

```json
{"type":"tool.confirmation_resolved","data":{"turn_index":1,"tool_call_id":"call_1","decision":"approved"}}
```

`decision` 的取值为 `approved`（确认）或 `rejected`（拒绝）。两个事件通过所属 Session、事件外层 `run_id`、`turn_index` 和 `tool_call_id` 关联。示例展示 `type` 和 `data`；完整事件还包含 `run_id`、`seq` 和 UTC `time`。

每次确认请求只能接受一次用户选择；重复或已失效的选择直接报错。确认、停止和工具启动须串行化，停止生效后不再启动工具。用户停止或服务重启时通过 `run.finished` 记录状态，不因停止或重启额外生成用户选择事件。

## 错误与停止

未知工具、无效参数、非字符串返回值和工具异常直接终止 Run，并记录失败状态。失败的工具不生成成功的 `tool.completed` 或 `toolResult`。

确认事件持久化后通过 SSE 推送。用户确认或拒绝时，先持久化选择事件，再执行相应动作；选择事件写入失败时不得执行删除。用户确认后继续执行；用户拒绝或停止时，删除不执行，Run 以 `stopped` 结束；此前已完成的工具操作保留。等待确认时服务重启，Run 按既定规则标记为 `interrupted`。

停止信号生效后，不再启动新工具。已经开始的同步工具可以继续到返回；执行完成后记录实际结果，再停止 Run。

工具调用和结果可能包含敏感数据。原始参数在记录及执行前检查已配置密钥；JSON 解码后及执行前检查完整参数模型，以识别嵌套字段中的密钥明文。结果在记录或推送前检查。检测到已配置密钥的明文时拒绝相关内容并终止 Run，错误记录不包含密钥。API key 和环境变量不写入记录；明文匹配无法识别未知或变形的秘密。
