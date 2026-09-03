# KUMA Python API 参考

简体中文 | [English](api-reference.md)

本文记录稳定的用户侧 Python API。参数类型、默认值、范围、副作用和失败语义均以当前实现为准。KUMA 的主要 API 使用仅关键字参数，调用时应保留参数名。

## `configure`

```python
from kuma import configure

credential_path = configure(api_key="dfx_your_key_here")
```

<!-- api-parameters:configure:start -->

| 参数 | 类型 | 必填/默认值 | 它控制什么、什么时候填写 |
| --- | --- | --- | --- |
| `api_key` | `str` | 必填 | 保存 KUMA 调用官方 Case/Judge 时使用的凭证。填写平台签发的 `dfx_...` 值；必须是可打印 ASCII，不能包含空白或控制字符，编码后最多 512 字节。完全本地运行不需要配置它。 |

<!-- api-parameters:configure:end -->

**返回值：** 原子写入的用户凭证文件绝对 `Path`。

**前置条件：** 必须传平台签发的完整 `dfx_...`，不能传脱敏后的展示值。若设置 `KUMA_CONFIG_HOME`，它必须是当前进程被允许写入的凭证目录。

**后置条件：** 成功后，返回路径存在且包含通过校验的 Key；最终文件采用原子替换，不会留下“写了一半”的正式文件。写入失败时会删除临时文件。

**异常：** Key 或凭证位置无效时抛 `ConfigurationError`；真实文件系统失败抛 `OSError`。

**副作用与安全：** 必要时创建凭证目录，但不发送网络请求。文件内是真实 Key，禁止打印、上传或提交到 Git。

## `create_run`

```python
from kuma import create_run

run = create_run(repo_path=".", requirement_path="requirement.md")
```

<!-- api-parameters:create_run:start -->

| 参数 | 类型 | 必填/默认值 | 它控制什么、什么时候填写 |
| --- | --- | --- | --- |
| `repo_path` | `str \| os.PathLike[str]` | `"."` | 指定“这次要测试哪个仓库”。KUMA 会读取该目录下的有界元数据，并在启用文件追踪时观察其中的文件变化。如果 Python 正在仓库根目录运行，保留 `"."` 即可。 |
| `requirement_path` | `str \| os.PathLike[str] \| None` | `None` | 指向描述“Agent 要做什么、KUMA 要测试哪些行为”的 UTF-8 文件。官方 Case 必须提供。Front matter 可包含 closed `strategy_group` 坐标和相对路径 `tool_capabilities` 文件，两者都会在 Provider I/O 前校验。只有自定义 Case Provider 明确不需要 Requirement 时才可省略。 |
| `case_provider` | `CaseProvider \| callable \| None` | `None` | 决定由谁生成测试步骤。保留 `None` 会向 KUMA 官方服务申请 Case；传入 callable 表示由你的程序在本地提供 Case。 |
| `judge_provider` | `JudgeProvider \| callable \| None` | `None` | 决定由谁评估全部步骤并生成最终报告。保留 `None` 使用官方 Judge；传入 callable 使用你自己的本地评估逻辑。`judge=False` 时不会使用它。 |
| `strategy` | `str` | `"auto"` | 保留仅使用未版本化 strategy ID 的服务兼容性。当前策略组应在 Requirement front matter 中填写精确 `id` 与 `version`。结构化声明与非默认旧值同时出现时会直接失败，避免产生歧义。 |
| `max_steps` | `int \| None` | `None` | 限制本次 Run 最多包含多少个测试步骤。例如填 `3`，Case 可以有 1、2 或 3 个步骤，并不保证一定生成 3 个。`None` 采用官方服务上限；自定义 Case Provider 必须填写正整数。显式值超过服务端公开上限会在生成 Case 前报错，KUMA 不会截断已返回的 Case。 |
| `judge` | `bool` | `True` | 控制最后一个 Input 提交后是否进行评估。保持 `True` 才会得到 `TestReport`；设为 `False` 只执行并记录 Case，`run.report` 会保持 `None`。 |
| `on_failure` | `str` | `"continue"` | 决定某一步被提交为 `failed`、`timeout` 或 `aborted` 后怎么办。`"continue"` 会继续交付下一个 Input；`"stop"` 会立即结束整个 Run。 |
| `allow_local` | `bool` | `False` | 允许在 Docker 外启动可信的本地开发 Run。它只绕过 Docker 要求，不会隔离 Agent、扩大文件权限，也不会关闭校验或隐私保护。 |
| `track_files` | `bool` | `True` | 让 KUMA 在每个 Input 前后比较仓库文件，从而告诉 Judge 哪些文件被创建、修改、删除或重命名。文件变化与评估无关或无法观察时可设为 `False`。 |
| `upload_diff` | `bool` | `False` | 除路径、哈希、大小和变化类型外，再把有界的实际文本改动加入 Evidence。只有 Judge 必须查看代码差异且仓库文本允许披露时才开启；要求 `track_files=True`。 |
| `save_local` | `bool` | `False` | 把每个已成功提交的 Submission 额外保存为 `.kuma/runs/<run_id>/` 下的 JSON，便于调试和审计。它只是本地副本，不能替代提交给官方 Judge。 |
| `allow_sensitive` | `bool` | `False` | 当普通 Evidence 被扫描器判断为可能敏感时，是否仍允许继续。默认应保持 `False`；只有人工确认内容可以披露时才开启，而且它永远不能让秘密进入 OTel Trace Evidence。 |
| `timeout` | `float` | `300.0` 秒 | 限制一次连接 KUMA 公网服务的 HTTP 请求最多等待多久。调小后单次网络失败会更快返回；它不限制 Case 生成或 Judge 的总等待时间。 |
| `operation_wait_timeout` | `float` | `600.0` 秒 | 限制一次官方 Case/Judge operation 连同轮询在内总共等待多久。超时后 KUMA 抛出可重试错误，并保留安全恢复信息，以便继续同一个 operation。 |
| `max_retries` | `int` | `2` | 设置一次瞬态 HTTP 失败后最多再尝试几次，允许 0–5。重试会复用同一个幂等键，不会故意创建第二个 Case/Judge operation。 |
| `api_key` | `str \| None` | `None` | 为“这一个 Run”提供官方服务凭证，用于临时覆盖环境变量或已保存凭证。`None` 时依次读取 `KUMA_API_KEY` 和用户凭证文件；Case/Judge 都是本地 Provider 时不需要 Key。 |
| `trace_evidence` | `TraceEvidenceCapture \| None` | `None` | 为本次 Run 指定一份 OTel Trace 采集器及其资源上限。需要显式控制时传入 `configure_trace_evidence()` 的返回值；`None` 时 KUMA 会尝试复用兼容的全局 Provider，没有则继续运行并记录非阻断 warning。 |
| `scan_strategy_group` | `bool` | `False` | 明确启用官方 Case 的本地保守策略组建议。KUMA 只比较 closed 声明能力与本次 Run 的内在 Runtime Evidence 能力，不执行工具，也不根据名称、描述、Schema、资源、访问方式或副作用猜测。只有唯一可靠匹配时才选择非默认组；同分或无匹配时使用目录精确默认组。Requirement 中的显式选择始终优先。 |

<!-- api-parameters:create_run:end -->

**返回值：** 处于 `ready` 状态的同步 `Run`。

**前置条件：** `repo_path` 必须是调用方明确允许 KUMA 检查的仓库。官方 Case 需要可读的 Requirement 和有效 Key。除非设置 `allow_local=True`，进程必须运行在支持的容器环境中；同一本地运行环境一次只能有一个 Run 持有 active-Run 锁。

**后置条件：** 返回的 Run 已持有该锁，并装入一个通过校验的 Case。若 `max_steps=N`，Case 可以有 1 到 N 个 Input，而不是必须恰好 N 个。获取运行资源后若初始化失败，KUMA 会先关闭资源并释放锁，再把异常抛给调用方。

**异常：** 配置、凭证、隔离、Provider、Case 或公网服务失败会抛出具体 `KumaError` 子类，并提供稳定的 `code`、`retryable` 和可选 `request_id`。

**副作用与安全：** 读取 Requirement 和有界仓库元数据，可能创建 `.kuma/`，官方 Provider 只调用公开 Backend。SDK 不直连 MCP、模型或数据库；自定义 Provider 在调用方进程内运行并继承该进程权限。

## `Run`

### `get_input`

<!-- api-parameters:get_input:start -->

| 参数 | 类型 | 必填/默认值 | 它控制什么、什么时候填写 |
| --- | --- | --- | --- |
| `full` | `bool` | `False` | 决定 Agent 能拿到多少信息。保持 `False` 只返回真正要执行的任务 payload；需要 run/case/input ID、序号、payload 类型、约束或扩展字段时设为 `True`，返回完整且不可变的 `KumaInput`。 |

<!-- api-parameters:get_input:end -->

**返回值：** 当前 payload 或不可变 `KumaInput`；全部 Input 已提交后返回 `None`。

**前置条件：** Run 必须为 `ready` 或已经是 `input_delivered`；拿到当前 Input 后必须先提交，才能请求下一个。

**后置条件：** 首次交付会把 `ready` 改为 `input_delivered` 并开始该步骤的 Evidence；重复调用返回同一个 Input，不推进状态和 history。

**异常与副作用：** 顺序错误抛 `InputProtocolError`，Evidence 初始化失败抛 `EvidenceCaptureError`。该方法可能启动有界采集，但不会调用 Judge 或追加 history。

### `submit`

<!-- api-parameters:submit:start -->

| 参数 | 类型 | 必填/默认值 | 它控制什么、什么时候填写 |
| --- | --- | --- | --- |
| `output` | 有限 JSON-compatible 值 | 省略 | 提交当前 Input 的 Agent 结果，Judge 会评估这个值。普通接入应明确传入。只有受支持 OTel instrumentation 已捕获真实最终 Agent/Workflow 输出时才能省略；显式传 `None` 不算成功结果。 |
| `status` | `str` | `"completed"` | 记录当前步骤实际如何结束：有可用结果用 `"completed"`，Agent 报错用 `"failed"`，超过执行期限用 `"timeout"`，主动终止用 `"aborted"`。该值也会触发 `on_failure` 的继续/停止策略。 |
| `error` | `str \| None` | `None` | 当 `status` 不是 `"completed"` 时，提供一段用户可读的失败摘要。它会进入 Submission Evidence，因此只能写安全概述，不能放 secret、文件正文或原始 traceback。 |
| `logs` | `list[str \| Path] \| None` | `None` | 指定哪些本地日志文件的“新增部分”要随本次 Submission 一起采集。KUMA 只读取有界增量，并继续做路径与敏感数据校验；不需要日志时保留 `None`。 |
| `wait` | `bool` | `True` | 让最后一次 `submit()` 同步等待 Judge，直到拿到报告或错误才返回。当前公共 API 必须保持 `True`，不提供后台轮询模式。 |

<!-- api-parameters:submit:end -->

**返回值：** 只有最后一次 Submission 完成 Judge 时返回 `TestReport`，其他情况返回 `None`。

**前置条件：** 当前必须有一个已交付但未提交的 Input。`completed` 必须有显式非 `None` output，或有受支持 OTel 捕获的真实最终输出；`logs` 路径必须位于允许的 Evidence 范围内。

**后置条件：** 成功时只追加一个不可变 history 项，Evidence offset、本地记录和 Trace 字节预算一起提交。校验或准备失败后 Input 仍保持已交付；最终 Judge 失败后已完成 history 可供 `judge()` 重试。

**异常：** 协议、输出、序列化和采集错误分别使用 `InputProtocolError`、`ValidationError`、`EvidenceCaptureError`；Judge 错误保留稳定 `KumaError` 类型。

**副作用与安全：** 可能读取有界文件/日志变化、原子保存本地 Submission，并同步调用 Judge。output、error、日志、diff 和 Evidence 可能进入公开 Judge 边界，禁止传入凭证、原始 traceback、Prompt 或未经批准的文件正文。

### `judge`

<!-- api-parameters:judge:start -->

| 参数 | 类型 | 必填/默认值 | 它控制什么、什么时候填写 |
| --- | --- | --- | --- |
| `wait` | `bool` | `True` | 让 `judge()` 一直等到最终报告或错误。公共 Python API 是同步接口，所以必须保持 `True`；最长等待时间通过 `create_run(operation_wait_timeout=...)` 控制。 |

<!-- api-parameters:judge:end -->

**返回值：** 通过校验的 `TestReport`；成功后重复调用返回同一报告。

**前置条件：** 所有 Input 都已有已提交 Submission，Run 为 `completed`，已配置 Judge Provider，并且 `wait=True`。

**后置条件：** 成功时保存报告并进入 `report_ready`；失败时恢复 `completed`，保留不可变 history、幂等 identity 和 pending operation 供重试。

**异常与副作用：** Run 未完成抛 `InputProtocolError`，`wait=False` 抛 `ConfigurationError`，Provider/服务失败保留稳定 `KumaError`。该调用同步执行 Judge；不能仅因轮询失败就创建第二个 operation。

### `cancel`

`cancel()` 没有参数。

**返回值：** `None`。

**前置条件：** Run 必须处于允许取消的生命周期状态；不能用 cancel 隐藏 failed 或正在提交的状态。

**后置条件：** 未完成 Run 进入 `cancelled`，活动 Evidence 被丢弃，运行资源和 active-Run 锁被释放；对 `cancelled` 或 `report_ready` 重复调用是幂等的。

**异常与副作用：** 非法状态抛 `InputProtocolError`。该方法删除经过校验的临时运行文件，但不会提交结果或调用 Judge。

### 只读属性

| 属性 | 类型 | 它告诉你什么 |
| --- | --- | --- |
| `run_id` | `str` | 标识这一次执行，可用于关联日志、本地产物和公开服务记录。 |
| `case_id` | `str` | 标识本次正在执行的公开 Case，可安全用于关联，但不会暴露 Private Rubric。 |
| `max_steps` | `int` | 表示最终生成的 Case 实际包含多少个步骤；至少为 1，且不会超过显式传入的 `create_run(max_steps=...)` 上限，参数为 `None` 时则不超过服务或本地默认上限。 |
| `state` | `RunState` | 告诉你现在允许做什么，例如获取 Input、提交、等待 Judge、已经完成或已取消。 |
| `history` | `tuple[HistoryItem, ...]` | 按执行顺序保存所有已成功提交的 Input 及对应 Submission；正在处理但尚未提交的步骤不在其中。 |
| `report` | `TestReport \| None` | `state` 变为 `report_ready` 后保存最终 Judge 结果；Judge 尚未完成或 `judge=False` 时为 `None`。 |
| `runtime_warnings` | `tuple[str, ...]` | 保存不会阻断 Run 的 Evidence 缺口代码，例如自动 Trace 不可用；可用它向用户提示采集不完整。 |
| `tool_capabilities_path` | `Path \| None` | 保存 Requirement 关联的本地能力文档绝对路径，供调用方检查；该路径永不上传。 |
| `tool_capabilities_provenance` | `str \| None` | 表示本地能力文档来源为 `user_declared`、`scanner_generated` 或 `None`；它不是对 Agent 行为的验证。 |

## `KumaClient`

不创建 Run、只读取鉴权配置时使用 `KumaClient`。

<!-- api-parameters:KumaClient:start -->

| 参数 | 类型 | 必填/默认值 | 它控制什么、什么时候填写 |
| --- | --- | --- | --- |
| `api_key` | `str \| None` | `None` | 用于读取账号权限、策略和 Judge 配置等公开信息。可以只给这个 client 传 Key；保留 `None` 时会读取 `KUMA_API_KEY`，再读取已保存凭证。 |
| `base_url` | `str` | KUMA 公开 URL | 决定这些 GET 请求发往哪个公开 Backend。普通用户保持默认即可；远程地址必须 HTTPS，本地集成可用 loopback HTTP，含用户名或密码的 URL 会被拒绝。 |
| `timeout` | `float` | `30.0` 秒 | 设置每次配置 GET 最多等待响应多久，超时就失败；它不控制 Case/Judge operation 的轮询总时长。 |
| `transport` | 公共 transport callable \| `None` | `None` | 用显式 callable 替换真实 HTTP，供测试或受控集成使用。普通应用应保留 `None`。 |

<!-- api-parameters:KumaClient:end -->

**前置条件：** 构造阶段会校验 URL、timeout 和发现的 Key，但不会发送请求；调用鉴权读取方法前必须有有效 Key。

**后置条件：** client 可以复用；`entitlements()`、`strategies()` 和 `judge_config()` 返回校验后的公开 mapping，`strategy_group_catalog()` 返回严格类型化目录；这些方法都不创建 Run。

**异常与副作用：** 构造配置错误抛 `ConfigurationError`；读取方法发送一次公开 Backend GET，可能抛 `KumaAuthenticationError`、`KumaPermissionError` 或 `KumaRateLimitError`。凭证发现可能读取环境变量或用户凭证文件；`repr(client)` 不含 Key，也不会直连 MCP、模型或数据库。

### `strategy_group_catalog`

`strategy_group_catalog()` 没有参数。

**返回值：** 不可变 `StrategyGroupCatalog`，包含 `catalog_release`、精确 `default` 声明和规范排序的 `groups`。每个 `StrategyGroup` 暴露 `id`、`version`、`display_name`、`description`、`required_capabilities`、`available` 与 `limits`；limits 包含 `max_steps` 和 `supported_difficulties`。

**前置条件：** client 已配置可接受的官方凭证。

**后置条件：** 完整公共目录已经通过 closed schema、边界、排序、唯一性和安全默认组校验。调用方可用 `group(declaration)` 精确查找坐标，用 `to_dict()` 取得分离后的规范 JSON。

**异常与副作用：** 执行一次带鉴权的公共目录读取。鉴权、权限或额度错误保留对应 `KumaError` 子类；畸形或旧格式数据抛 `ValidationError`。它不会创建 Case 或运行本地建议。

## 策略组 API

CLI 与 Requirement 工作流见[策略组指南](strategy-groups.zh-CN.md)。

| 公开名称 | 接受输入或暴露字段 | 结果与失败行为 |
| --- | --- | --- |
| `StrategyGroupDeclaration` | 精确 `id` 和 `version`；`to_dict()` 会加入 `kuma.strategy_group_selection.v1`。 | 不可变、可直接写入 Requirement 的坐标。 |
| `StrategyGroup` | `id`、`version`、`display_name`、`description`、`required_capabilities`、`available` 和组 `limits`。 | 不可变目录条目；`coordinate` 返回 `(id, version)`，`to_dict()` 返回分离 JSON。 |
| `StrategyGroupCatalog` | `catalog_release`、精确 `default` 和排序后的 `groups`。 | `group(declaration)` 返回精确条目或 `None`；`to_dict()` 返回规范目录 JSON。 |
| `ResolvedStrategyGroup` | 选中的 `group`、`selection_source` 和 `catalog_release`。 | `to_declaration()` 返回 Requirement 对象；`to_wire()` 返回 closed 公共解析结果。 |
| `validate_strategy_group_declaration(value)` | 只含 `schema_version`、`id` 和 `version` 的普通 mapping。 | 返回 `StrategyGroupDeclaration`；未知字段、版本或无效文本抛 `ValidationError(code="strategy_group_invalid")`。 |
| `validate_strategy_group_catalog(value)` | 完整 closed 目录 mapping。 | 返回 `StrategyGroupCatalog`；字段、排序、边界、坐标或默认组畸形时直接拒绝。 |
| `validate_strategy_group_wire_selection(value)` | 含 schema 版本、组 ID/版本、来源与目录版本标识的完整解析 mapping。 | 返回分离后的公共 mapping；无效或多余字段直接拒绝，主要用于高级 Provider 边界。 |

常量 `STRATEGY_GROUP_SELECTION_SCHEMA_VERSION` 与 `STRATEGY_GROUP_CATALOG_SCHEMA_VERSION` 暴露两个接受版本。这些值对象和校验函数不执行网络、文件系统、Agent 或模型操作。

## Agent 能力 API

Closed JSON schema、CLI 流程、Requirement 路径规则和隐私边界见 [Agent 工具能力指南](agent-tool-capabilities.zh-CN.md)。

| 公开名称 | 输入 | 返回值与副作用 |
| --- | --- | --- |
| `scan_agent_tools(tools)` | 由 1–100 个普通工具 mapping 组成的 list 或 tuple。 | 返回来源为 `scanner_generated` 的不可变 `AgentCapabilities`；不检查框架对象，也不执行工具。 |
| `validate_agent_capabilities(value)` | 完整的普通 `kuma.agent_tool_capabilities.v1` mapping。 | 返回已校验并规范排序的 `AgentCapabilities`；无效、超限或敏感数据直接拒绝。 |
| `load_agent_capabilities(path)` | 大小不超过文档边界的 UTF-8 JSON 文件。 | 读取并校验一个文件，返回 `AgentCapabilities`。 |
| `save_agent_capabilities(document, path)` | mapping 或 `AgentCapabilities`，以及父目录已存在的显式目标路径。 | 重新校验并原子写入规范 JSON，返回解析后的 `Path`。 |
| `scan_agent_tool_manifest(path)` | 显式 UTF-8 scanner 输入 JSON manifest。 | 只读取该文件并返回生成的 `AgentCapabilities`；不导入 Agent、不遍历仓库、不执行工具，也不联网。 |

`AgentCapabilities`、`ToolCapability` 和 `ResourceScope` 是不可变公开值，均提供分离后的 `to_dict()`。`AGENT_CAPABILITIES_SCHEMA_VERSION` 表示接受的文档版本。加载或保存可能抛出 `ValidationError` 或 `SensitiveDataError`；这些 API 均不上传文档。

## OpenTelemetry

导入 `kuma.otel` 前安装 `kuma-defuzex[otel]`。

<!-- api-parameters:configure_trace_evidence:start -->

| 参数 | 类型 | 必填/默认值 | 它控制什么、什么时候填写 |
| --- | --- | --- | --- |
| `tracer_provider` | OTel SDK Provider \| `None` | `None` | 指定 KUMA 从哪个同进程 OTel Provider 接收已结束的 span。应用使用非全局 Provider 时明确传入；`None` 使用当前全局 Provider。KUMA 只添加 processor，绝不会替换或重置它。 |
| `logger_provider` | OTel SDK Provider \| `None` | `None` | 指定现有同进程 OTel LoggerProvider，用于采集有界的原生日志元数据。需要显式日志采集时传入；`None` 在显式模式下不附加日志采集。KUMA 不会替换它。 |
| `limits` | `TraceEvidenceLimits \| None` | `None` | 控制一个 Run 最多保留多少 Trace 数据。需要更严格的内存或隐私预算时传入自定义限制；`None` 使用下方有界默认值。 |

<!-- api-parameters:configure_trace_evidence:end -->

**返回值：** 供 `create_run(trace_evidence=...)` 使用的 `TraceEvidenceCapture`。

**前置条件：** 已安装 `otel` extra，并配置了可添加 span processor 的同进程 OTel SDK Provider；非全局 Provider 必须显式传入。

**后置条件：** 选定 Provider 上新增一个 KUMA processor，返回的 capture 可以把已结束 span 关联到 Run；原有 instrumentation 和 exporter 保持不变。

**异常与副作用：** Provider 或限制无效时抛 `ConfigurationError`。注册操作会修改所选 Provider，显式管理的 Provider 应只调用一次。只保留有界 allowlist 数据；Prompt、completion、源码、原始日志、凭证和 Private Rubric 始终排除。

<!-- api-parameters:TraceEvidenceLimits:start -->

| 参数 | 类型 | 必填/默认值 | 达到上限后会怎样 |
| --- | --- | --- | --- |
| `max_spans` | 正 `int` | `200` | 一个 Run 保留到该数量后，后续已结束 span 会被丢弃，并在 Evidence 中记录 dropped，而不是让内存无限增长。 |
| `max_attributes` | 正 `int` | `32` | 每个 span 最多保留这么多个安全 allowlist 属性；其余属性被丢弃并计数。无论数字多大，敏感属性仍会被拒绝。 |
| `max_events_per_span` | 正 `int` | `20` | 每个 span 最多保留这么多个安全 OTel event；更晚的 event 会被丢弃并记录。 |
| `max_text_length` | 正 `int` | `256` 字符 | 每个允许保留的文本值超过该 Unicode 字符数后会被截断，并记录 truncated 状态。 |
| `max_total_bytes` | 正 `int` | `512000` 字节 | 一个 Run 的全部已提交 Trace envelope 紧凑 JSON 合计不能超过该值；KUMA 会丢弃或截断 Trace 数据来守住预算，但该值本身必须能容纳最小合法 envelope。 |
| `max_log_records` | 正 `int` | `200` | 每个步骤最多保留的规范化 OTel 日志记录数；超出部分会丢弃并记录。 |
| `max_log_bytes` | 正 `int` | `128000` 字节 | 一个 Run 中已提交的结构化 OTel 日志 artifact 总字节上限；不会保留原始日志正文。 |

<!-- api-parameters:TraceEvidenceLimits:end -->

**前置条件：** 每项都是正整数，且 `max_total_bytes` 足以容纳必需 envelope。

**后置条件：** 生成不可变限制对象；超额 Trace 会按明确原因丢弃或截断，而不会越过预算。提高容量不会扩大隐私 allowlist。

## 公共结果契约

主要不可变类型从 `kuma` 导出：

| 类型 | 重要字段与含义 |
| --- | --- |
| `KumaInput` | `run_id`、`case_id`、`input_id`、从零开始的 `index`、`payload_type`、冻结的 `payload`、公开 constraints、schema version 和公开 extensions。 |
| `Submission` | 关联 ID、步骤终态 `status`、JSON output/error、采集完整性、有界 logs/file Evidence、dropped/missing 计数、schema version 和 extensions。 |
| `HistoryItem` | 一个 `KumaInput` 与 ID 完全匹配的 `Submission`。 |
| `TestReport` | `report_id`、`run_id`、`status`（`pass`、`issue` 或 `insufficient_evidence`）、confidence、stop reason、公开 issues/evidence gaps 和 extensions。 |
| `CaptureStatus` | file snapshot/diff、logs、sensitive scan、traces 的完整性；每项为 `complete`、`partial`、`failed` 或 `skipped`。 |

这些对象不包含 Private Rubric、Prompt、模型设置或 Core 记录。Runtime Evidence v1 仅含哈希；服务端明确协商 v2 后，可携带经过大小与敏感检查的 completed Agent 最终输出。两种格式见 [Runtime Evidence 合同](runtime-evidence.md)。

## 错误字段

普通 SDK 失败统一捕获 `KumaError`。`str(exc)` 是安全的用户文案；程序判断使用 `exc.code`、`exc.retryable` 和 `exc.request_id`。`exc.details` 是有界公开 mapping，也只应通过应用自己的 allowlist 记录。
