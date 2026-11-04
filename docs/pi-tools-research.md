# Pi 基础工具与独立工具设计调研

调研时间：2026-11-04。本文只包含源码事实和设计提案；项目工具尚未实现，也未接入 agent loop。

## 调研范围与项目现状

- Pi 源码位置：`D:/Repository/pi`，本次依据 commit `42a3497d03ad17e308a2299fa824727894f2c0ec`，`packages/coding-agent/package.json` 标示版本 `1.1.0`。检查时本地 `main` 显示落后 `origin/main` 615 个提交；以下结论针对该 checkout。[P1]
- 调研期间观察到当前项目出现 `backend/pyproject.toml`、`frontend/package.json` 和 `docs/agent-runtime/` 等脚手架及设计材料。后端声明 Python `>=3.14`、FastAPI 和 uvicorn；尚未发现工具业务代码或测试。项目目录仍在变化。
- 当前 Python 环境可导入 Pydantic `2.14.2`；`backend/pyproject.toml` 尚未直接声明它。`docs/agent-runtime/03-tool-design.md` 提议使用 TypeScript 的 Pi AgentTool 和 TypeBox；当前后端脚手架采用 Python，无法直接导入这些 TypeScript 类型。本文以 Python 独立工具作为具体提案，保留后续适配其他运行时的空间。
- 以下 JSON 均为**提案示例**，搜索与天气示例使用合成数据。在线文档按 2026-11-04 所见内容引用。

## 一、Pi 内置工具有哪些

该版本注册了八个内置工具：`read`、`bash`、`powershell`、`edit`、`write`、`grep`、`find`、`ls`。无配置时启动的基础集合为 `read`、`bash`、`edit`、`write`；其余工具可以配置开启。SDK 还提供 `read`、`grep`、`find`、`ls` 的只读集合。具体会话还可能加载扩展或自定义工具。[P1]

### 1. `read`：读取文件

- 参数：`path`，可选 `offset`、`limit`；`offset` 从 1 开始。
- 支持文本及图片；文本输出默认最多 2,000 行或 50 KiB，触发截断时提示继续读取的位置。起始行越界会报错。
- 实现会先把文本文件整体读进内存，再切分行和截断输出；输出上限并不限制文件本身的读取量。
- 单行超过字节上限时，Pi 给出提示，不返回这一行的部分内容。[P2]

### 2. `write`：创建或完整覆盖文件

- 参数：`path`、`content`。
- 创建缺失的父目录，以 UTF-8 写入；目标文件已存在时直接覆盖。
- 适合创建文件和完整重写。覆盖具有数据损失风险，调用方应明确授权。[P3]

### 3. `edit`：定点修改文件

- 参数：`path`、`edits`；每项包含 `oldText` 和 `newText`。一轮可以修改同一文件中多个不重叠位置。
- 所有位置都相对于原始内容匹配。匹配缺失、重复、重叠、空 `oldText` 等情况报错；全部位置校验后才执行写入。
- Pi 提示调用方提供精确文本。源码实现先尝试精确匹配，也会在必要时对部分空白和 Unicode 字符做归一化匹配。它保留 BOM 和原有换行风格，结果附带 diff/patch。[P4]
- `write` 与 `edit` 共享进程内的按文件串行队列；已有路径会通过 `realpath` 合并符号链接别名。队列无法约束其他进程或 `bash` 写入，写入前完成校验也不等于文件系统事务。[P5]

### 4. `grep`：按内容搜索

- 参数：`pattern`，以及可选的 `path`、`glob`、`ignoreCase`、`literal`、`context`、`limit`。
- 默认使用正则；可切换字面匹配。结果包含路径、行号和匹配内容，默认最多 100 条、50 KiB，长行截断至 500 字符。
- 默认调用 ripgrep `rg`，遵循忽略规则；无匹配是正常结果。子进程参数与搜索词分开传递，并用 `--` 终止选项解析。[P6]

### 5. `find`：按文件名或路径查找

- 参数：`pattern`、可选 `path` 和 `limit`；`pattern` 为 glob，如 `**/*.py`。
- 默认返回最多 1,000 个相对路径或 50 KiB，遵循忽略规则；默认调用 `fd`，也支持提供自定义 glob 操作。[P7]

### 6. `ls`：列出目录

- 参数：可选 `path`、`limit`。
- 展示当前目录的直接子项，包含点开头的文件；目录名附 `/`。默认最多 500 项或 50 KiB。[P8]

### 7. `bash` 与 `powershell`：执行系统命令

- `bash` 接收 `command` 和可选的秒级 `timeout`；源码默认没有超时。`powershell` 是另一个可选命令执行工具。
- `bash` 合并 stdout/stderr，返回末尾最多 2,000 行或 50 KiB；截断时 Pi 可把完整输出写到临时文件。非零退出会作为工具错误报告。[P9]
- 命令执行可以读取、修改文件和访问进程环境；工作目录本身不提供隔离。项目规则禁止读写 `/tmp`，自建工具的任何中间文件需使用已有 gitignore 规则覆盖的目录。

### 8. 工作目录与安全边界

Pi 的文件工具按当前工作目录解析相对路径，也接受绝对路径。当前源码中的路径解析不是工作区沙箱；`read-only` 工具集合约束的是暴露的工具能力，文件系统权限仍由宿主决定。[P10]

`grep`、`find` 默认依赖 `rg`、`fd`。该 Pi 版本会查找工具，缺失时可能尝试下载；离线和供应链条件会影响执行。本机 PATH 当前能找到两者，部署环境尚未验证。[P6][P7][P11]

## 二、Pi 的工具契约与示例

Pi 的工具定义包含名称、描述、参数 schema 和执行函数。执行结果包含面向模型的 `content` 与供程序、展示或状态使用的 `details`；还可声明结构化输出 schema 并返回 `structuredContent`。`details` 可能进入日志或会话存储，仍需访问控制。执行错误可通过抛异常或 `isError` 表达。[P12][E1]

- **Calculator 示例**：`packages/agent/test/utils/calculate.ts` 定义了 `calculate(expression)`，测试辅助代码使用 `new Function` 执行字符串。它展示工具注册形态；将不可信输入交给该实现会执行任意 JavaScript，不能直接用于项目。[P13]
- **Todo 示例**：`examples/extensions/todo.ts` 提供 `list`、`add`、`toggle`、`clear`。状态保存在内存中，并从当前 session 分支的工具结果 `details` 重建。这个恢复机制依赖 Pi session；示例中的部分错误只写入文本和 details，没有设置 `isError`。[P14]
- **搜索示例**：`examples/extensions/truncated-tool.ts` 展示输出截断；其中将参数拼到 shell 命令的做法存在注入风险，项目应使用参数数组调用进程。[P15]
- 在所检查的 Pi 示例范围内，未找到完整的 `read_docs` 或天气服务工具；`weather` 也出现在 schema 测试数据中，不能据此认定存在天气服务实现。

## 三、本项目的独立工具契约提案

以 `backend/` 的 Python 脚手架为实现环境。工具作为可直接调用的 Python 函数或对象，当前不依赖模型、agent loop、Pi session 或 MCP server。

每个工具具备：

1. `name`：稳定且唯一的名称。
2. `description`：说明用途、数据范围及限制。
3. `input_schema`：从 Pydantic 输入模型生成 JSON Schema；在执行入口校验必填项、类型、额外字段和长度范围。
4. `execute(args)`：接收已校验参数，返回可 JSON 序列化的结构化字典。

各工具对合法的空查询结果返回空数组；参数错误、越权、文件错误、上游故障等应直接抛出明确异常，保留失败语义。公开接口上线前需确定错误脱敏和鉴权边界，避免泄露密钥或宿主机敏感路径。

Pydantic 默认会对部分输入做类型转换；需要严格类型的字段应明确开启严格校验。若后续直接使用 Pydantic，项目应将它声明为直接依赖并锁定版本。[E2]

后续接入模型时，使用适配层把本项目的 schema、结果和错误映射到选定提供商的协议。不同提供商对 JSON Schema 的严格模式有各自限制；例如 OpenAI 的 strict 模式要求对象禁止额外属性，并将属性列为 required，允许空值的字段需显式表达。[E3]

## 四、五个自定义工具

### 1. `calculator`：四则计算

**提案**

- 输入 `operation: add | subtract | multiply | divide`，以及十进制字符串 `a`、`b`。
- 使用 Python 标准库 `decimal.Decimal`。每次运算使用明确的局部 context：28 位有效数字、`ROUND_HALF_EVEN`，显式启用除零、无效运算和溢出异常；结果以字符串返回，避免 JSON 数字转成二进制浮点后丢失十进制语义。[E4]
- 数字字符串最多 64 个字符；输入和结果的十进制数量级指数限制为 -100 至 100，零单独允许。除零、NaN、Infinity 与非法数字直接报错。28 位精度会对超出精度的结果舍入，具体货币精度和业务舍入规则需由业务另行规定。
- 每次调用执行一次二元运算；复杂表达式需要拆成多次计算。Pi 测试工具的字符串执行能力不进入该契约。

```json
{"operation":"add","a":"0.1","b":"0.2"}
```

```json
{"value":"0.3"}
```

**取舍**：表达式字符串可以覆盖括号和优先级，需要成熟数学解析库以及运算量、安全隔离等限制。math.js 官方也提示任意表达式可能带来资源耗尽等风险；当前提案使用结构化运算参数，避免引入表达式解析器。[E5]

### 2. `search`：模拟网络搜索

**提案**

- 输入 `query` 和 `limit`；`limit` 默认 5，限制在 1–10。
- 在受控的预置样例集合中，对标题与摘要执行确定性的大小写无关子串匹配；无命中返回空数组。
- 输出包含 `source: "mock"` 和结果数组；结果字段为 `title`、`url`、`snippet`。工具描述明确数据为模拟内容，不能将其标为联网结果或实时事实。

```json
{"query":"退货","limit":5}
```

```json
{"source":"mock","results":[{"title":"示例退货政策","url":"https://example.com/return","snippet":"示例内容"}]}
```

`example.com` 地址与内容仅用于说明字段。实际接入外部搜索时，需确定供应商、密钥、超时、限流、引用和内容可信度；搜索摘要不能证明原文内容。Pi 的 `grep`、`find` 属于本地文件搜索，本节的 `search` 表示网络搜索接口。[E6]

### 3. `read_docs`：读取授权文档

**提案**

- 输入 `doc_id`、可选 `offset` 和 `limit`。宿主维护 `doc_id → 文档路径` 的固定映射，调用者只能读取已登记的文档。
- 范围限定为受控文档目录中的 UTF-8 `.md`、`.txt` 普通文件；按行分页，返回 `content`、起始行、`next_offset` 和 `truncated`。
- 对可读行数和字节数设上限。单行超过上限时明确报错，避免续读位置跳过内容。
- 文档文本属于数据；文档里的指令不自动获得执行权限。

假设 `guide` 已登记，且文档在第二行之后仍有内容：

```json
{"doc_id":"guide","offset":1,"limit":2}
```

```json
{"doc_id":"guide","content":"# Guide\nWelcome","start_line":1,"next_offset":3,"truncated":true}
```

`read_docs` 的固定文档清单适合公开项目文档；Pi 的 `read` 面向更宽泛的文件读取。路径限制仍需处理符号链接、Windows junction 和并发替换。[E7]

### 4. `todo`：管理一份任务清单

**提案**

- `add(title)`：增加任务，返回任务 ID。
- `list`：返回当前任务及状态。
- `set_status(id, status)`：明确设置 `pending`、`in_progress` 或 `completed`。
- `delete(id)`：删除指定任务。
- 每份清单由独立工具实例持有，ID 在该实例内递增。实例内用锁串行保护读写并返回快照；调用方负责实例归属，避免不同用户共用同一份状态。
- 状态只保存在内存，进程退出即丢失；多进程共享和重启恢复未包含在该契约中。

```json
{"action":"add","title":"整理资料"}
```

```json
{"item":{"id":1,"title":"整理资料","status":"pending"}}
```

标题非空且限长；更新或删除不存在的 ID 报错。设置具体状态具有重复调用后的确定结果。若要求持久化和跨进程并发，需明确存储所有权、事务和冲突策略；SQLite 事务是可研究的本地方案。[E8]

### 5. `weather`：自定义天气数据

**提案**

- 输入 `location`；宿主提供城市名到天气记录的配置映射。
- 输出城市、天气描述、摄氏温度、`source: "custom"`、`is_live: false`。地点缺失或无记录时明确报错。
- 数据仅代表用户配置的模拟记录，无实时天气含义。示例城市和数值均为虚构数据。

```json
{"location":"示例城"}
```

```json
{"location":"示例城","condition":"晴","temperature_c":24,"source":"custom","is_live":false}
```

接入真实天气服务时，应记录来源、天气数据有效时间、时区和单位，约束网络目标及超时。Open-Meteo 的预测接口使用经纬度；地名解析可能返回多个同名地点。商业使用条件和归因要求需在接入时核对。[E9]

## 五、本项目文件工具建议

以下为项目侧提案，供决定是否实现；Pi 行为见第一节。

- `read(path, offset, limit)`：仅允许受控根目录内的 UTF-8 普通文件；返回行号、内容、截断标志和续读位置。明确限制单文件读取量及单行大小。
- `write(path, content, overwrite=false)`：创建文件；完整覆盖需显式传入 `overwrite=true`。路径和写入权限由宿主决定，输出不得泄露敏感绝对路径。
- `edit(path, edits)`：每项提供 `old_text`、`new_text`；对原文做唯一、非空、不重叠的精确匹配，全部校验通过后写入。保留原有 BOM 和换行风格，返回变更摘要。
- `grep(pattern, path, glob, limit)`：内容搜索，默认字面匹配；结果含相对路径、行号和片段。需要正则时使用成熟搜索工具的正则实现，并限制结果和运行时间。
- `find(pattern, path, limit)`：按 glob 查找路径；`ls(path, limit)`：列出直接子项。结果返回截断标志。
- `grep`、`find` 若需兼容 `.gitignore`，可调用环境提供的 `rg` / `fd`；使用参数数组与 `--`，校验退出状态，避免 shell 拼接。实际部署时明确安装与版本，禁止依赖运行时自动下载。
- 通用 `bash`、`powershell` 具有任意命令执行能力；当前独立工具提案不开放它们。需要开放时，先确定隔离环境、权限、密钥暴露、资源限制和审计策略。

文件根目录约束需要同时处理绝对路径、`..`、盘符、UNC 路径、符号链接与 Windows reparse point。`resolve` 后检查目录归属能够阻挡部分路径穿越；攻击者若能在校验和打开之间替换路径，仍存在 TOCTOU 风险。面向不可信调用时需要操作系统级隔离或经过平台验证的安全打开方式，且单独验证 Windows 实现。[E7]

文件写入应考虑覆盖确认、并发写、失败和进程中断。单进程内可串行化同一文件的读改写；跨进程写入需要额外协调。采用同文件系统原子替换时，仍需验证目标文件系统、权限、元数据与崩溃持久化语义。

## 六、验证清单

本节为实现验收要求，**当前均未运行**。

- 通用：正确参数、缺失字段、多余字段、错误类型、边界值、超长输入、输出序列化和异常传播；错误消息不得包含密钥。
- `calculator`：`0.1 + 0.2`、负数、重复小数的舍入、除零、NaN、Infinity、非法数字、指数和长度上限。
- `search`：固定输入得到确定结果、空结果、`limit` 上限、模拟来源标识；测试不访问网络。
- `read_docs`：无效 `doc_id`、分页首尾、超长行、文件不存在、无权限、非 UTF-8、符号链接和受控根目录边界。
- `todo`：增、查、状态设置、删除、不存在的 ID、重复设置、实例隔离和并发调用；明确验证重启后内存状态丢失。
- `weather`：已配置地点、未知地点、非法温度和模拟来源标识；自定义数据不被标记为实时天气。
- 文件工具：读写、覆盖保护、多个 edit 的唯一性与重叠、BOM/换行、忽略规则、空搜索、长输出、路径穿越、符号链接、并发修改与失败时的文件完整性。
- 测试产生的中间文件仅放在已由项目 `.gitignore` 明确覆盖的目录；当前项目尚需建立对应规则。

## 七、风险、分歧与下一步

- **语言决策**：当前 Python 后端脚手架与现有文档中的 TypeScript/Pi AgentTool 提案存在差异。本文推荐 Python 原生工具契约；实际开发前需确认后端语言和 Pi 的接入边界。
- **计算器范围**：外部调研提出表达式及括号支持；本提案提供四则二元运算，复杂表达式能力属于明确缺口。
- **数据与状态**：`search`、`weather` 使用可识别的自定义模拟数据；`todo` 为进程内状态。实时性、持久化和多实例共享均未得到实现或验证。
- **安全边界**：Pi 的 cwd、忽略规则和只读工具集合均不能单独承担宿主隔离；跨平台安全文件访问和授权策略仍需验证。
- **研究可信度**：本地源码与示例事实为高置信度；实际部署权限、供应商条款、跨平台并发和性能为待验证事项。Pi 测试、真实天气请求、模型调用与本项目工具实现均未运行。
- **推荐下一步**：确认工具范围和数据边界，按第三至五节契约在 Python 后端直接实现，并执行第六节的离线验收；本次交付仅为报告。

## 八、资料与源码定位

本地 Pi 源码路径均相对于 `D:/Repository/pi/`，行号对应本次 checkout。

- [P1] `packages/coding-agent/src/core/tools/index.ts:90-187`；`packages/coding-agent/src/core/settings-manager.ts:82-140`；`packages/coding-agent/package.json:1-5`。
- [P2] `packages/coding-agent/src/core/tools/read.ts:15-18,86-211`；`tools/truncate.ts:1-13,72-115`。
- [P3] `packages/coding-agent/src/core/tools/write.ts:11-18,44-92`。
- [P4] `packages/coding-agent/src/core/tools/edit.ts:21-49,102-137,141-211`；`tools/edit-diff.ts:207-242,247-362`。
- [P5] `packages/coding-agent/src/core/tools/file-mutation-queue.ts:16-59`。
- [P6] `packages/coding-agent/src/core/tools/grep.ts:21-32,70-78,115-166,197-255`。
- [P7] `packages/coding-agent/src/core/tools/find.ts:26-32,70-80,109-128,171-216`。
- [P8] `packages/coding-agent/src/core/tools/ls.ts:11-14,54-64,81-160`。
- [P9] `packages/coding-agent/src/core/tools/bash.ts:22-43,96-163,186-224,243-260,263-410`。
- [P10] `packages/coding-agent/src/core/tools/path-utils.ts:40-50`；`packages/coding-agent/examples/sdk/05-tools.ts:14-20`。
- [P11] `packages/coding-agent/src/utils/tools-manager.ts:71-103,258-286,345-396`。
- [P12] `packages/agent/src/types.ts:422-499`；`packages/coding-agent/docs/extensions.md` 的 State Management、Custom Tools 章节。
- [P13] `packages/agent/test/utils/calculate.ts:10-32`。
- [P14] `packages/coding-agent/examples/extensions/todo.ts:17-35,100-212`。
- [P15] `packages/coding-agent/examples/extensions/truncated-tool.ts:33-133`。
- [E1] [MCP Tools 规范 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)：工具输入、结构化输出与错误语义；本文未要求实现 MCP。
- [E2] [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/)：严格校验与默认类型转换。
- [E3] [OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)：工具 schema、执行与 strict 模式。
- [E4] [Python decimal](https://docs.python.org/3/library/decimal.html)：精度、舍入、局部 context 和异常。
- [E5] [math.js expression security](https://mathjs.org/docs/expressions/security.html)：表达式执行及资源风险。
- [E6] [OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search)；[ripgrep GUIDE](https://github.com/BurntSushi/ripgrep/blob/master/GUIDE.md#automatic-filtering)：网络搜索与本地搜索的范围和过滤。
- [E7] [Python Path.resolve](https://docs.python.org/3/library/pathlib.html#pathlib.Path.resolve)；[OWASP 路径穿越防护](https://cheatsheetseries.owasp.org/cheatsheets/Symfony_Cheat_Sheet.html#directory-traversal)：规范化、目录约束及其前提。
- [E8] [SQLite Atomic Commit](https://www.sqlite.org/atomiccommit.html)：事务原子性及相关条件。
- [E9] [Open-Meteo Forecast API](https://open-meteo.com/en/docs)、[Geocoding API](https://open-meteo.com/en/docs/geocoding-api)、[Pricing](https://open-meteo.com/en/pricing)、[License](https://open-meteo.com/en/license)。
