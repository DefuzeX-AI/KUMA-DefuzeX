# KUMA Python SDK 指南

Evidence 容量：默认 Trace 总预算为每 Run 8 MiB；span、attribute、event 上限及
透明丢弃计数继续生效。Agent 输出 canonical JSON 上限 4 MiB，单份 Runtime
Evidence 上限 5 MiB，含元数据和分隔符的完整 multipart 上限 8 MiB。
Backend 更小的限制仍有效，输出不会截断。JSON 引号及转义也计入：ASCII 字符串
加两个引号前最多 4,194,302 字符，Unicode 转义可能占更多字节。
在源码目录运行 `python tools/verify_evidence_capacity.py` 可离线验证容量。

[English](sdk-guide.md) | [简体中文](sdk-guide.zh-CN.md)

本文是 KUMA 配置与接入的规范用户指南。Python 包、CLI 和环境变量使用 `kuma` / `KUMA_*`；版本化 `defuzex.*` wire schema 为兼容服务端保持不变。

## 安装

KUMA 支持 Python 3.10 至 3.14。请先创建隔离环境：

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install "kuma-defuzex==0.1.0"
```

Linux 或 macOS：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "kuma-defuzex==0.1.0"
```

按需安装 OpenTelemetry 能力：

```bash
python -m pip install "kuma-defuzex[otel]==0.1.0"
```

贡献者请按 [`CONTRIBUTING.md`](../CONTRIBUTING.md) 使用可编辑开发环境。

## 本地快速开始

CLI quickstart 会在 SDK 自有临时目录中执行确定性的精确匹配检查，不读取用户仓库，也不需要账号、API Key、Docker 或网络：

```bash
kuma quickstart
```

`kuma quickstart --fail-demo` 可验证确定性失败路径。另有一个使用自定义 Case Provider、关闭 Judge 的完整本地 `Run`：

```bash
python examples/minimal_local.py
```

## 配置

### API Key

官方 Case 或 Judge Provider 需要以 `dfx_` 开头的 KUMA API Key。请通过进程环境或用户凭证存储提供，不要写入源码、Notebook 输出、日志或 Git。

Windows PowerShell：

```powershell
$env:KUMA_API_KEY = "dfx_your_key_here"
kuma whoami
```

Linux 或 macOS：

```bash
export KUMA_API_KEY="dfx_your_key_here"
kuma whoami
```

SDK 也可在不访问网络的情况下验证并原子保存 Key：

```python
from kuma import configure

credential_path = configure(api_key="dfx_your_key_here")
print(credential_path)
```

凭证优先级为：`create_run(api_key=...)`、`KUMA_API_KEY`、用户凭证文件。

| 环境变量 | 用途 |
|---|---|
| `KUMA_API_KEY` | 官方 Provider 使用的凭证 |
| `KUMA_CONFIG_HOME` | 覆盖用户凭证目录 |
| `KUMA_BASE_URL` | 覆盖允许的公开或 loopback API 地址；非 loopback 地址必须使用 HTTPS |

### Agent Profile 文件

官方 Case Provider 要求显式提供 UTF-8 Agent Profile 文件，包含 YAML front matter 和三个章节：

策略组始终决定主要测试能力、领域和方法。Agent Profile 只提供被测 Agent、生产场景、预期行为与禁止边界等上下文；其中的自然语言不会选择、替换或覆盖策略组。若 Profile 省略 `strategy_group`，KUMA 使用目录中精确的默认组。

```markdown
---
agent_description: A repository maintenance agent
input_type: text
---

## Production Use Scenario

Maintain a Python repository without changing its public interface.

## Behaviors to Test

Diagnose the requested defect, apply a bounded fix, and run relevant checks.

## Known Limitations or Prohibited Behaviors

Do not read credentials or access paths outside the repository.
```

`agent_description`、`input_type` 和三个标题均为必填。官方 Case 当前接受文本 Input；结构化 Input 需要自定义 Case Provider，并通过 `input_schema` 声明本地验证的 JSON Schema。

### 策略组与 Agent 能力

已鉴权用户可在编辑 Agent Profile 前查询并校验当前公共策略组目录：

```bash
kuma strategies list
kuma strategies list --output strategy-groups.json
```

通过 closed `strategy_group` front matter 写入选定组的 `id` 和精确 `version`。省略时使用目录中的精确默认组；显式选择无效或缺少所需 Evidence 能力时会直接拒绝。`scan_strategy_group=True` 只是明确启用本地保守建议，默认保持关闭。Agent Profile schema、CLI 参数、类型化 Python API、默认行为和隐私边界详见[策略组](strategy-groups.zh-CN.md)。

可选的 `tool_capabilities` 相对路径可以关联经审查的本地能力文档。可用 `kuma tools scan` / `kuma tools validate` 创建或校验，也可使用等价 Python helper。该文件不会上传；它是用户可控声明，只有 closed Evidence 能力集合可以参与本地建议。Schema、边界、CLI、Python API 和路径规则详见 [Agent 工具能力](agent-tool-capabilities.zh-CN.md)。

### 接入 Agent

用户负责执行 Agent，KUMA 负责同步 `Run` 协议。将下面的确定性函数体替换为现有 Agent 调用：

```python
from typing import Any

from kuma import create_run


def execute_agent(test_input: Any) -> dict[str, Any]:
    return {"result": str(test_input)}


run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
    allow_local=True,  # 仅用于可信的本地开发。
)

report = None
while (test_input := run.get_input()) is not None:
    report = run.submit(execute_agent(test_input))

print(run.state)
print(report)
```

`get_input()` 返回兼容 JSON 的 payload；`get_input(full=True)` 返回不可变的 `KumaInput`。成功的 Submission 必须包含有限且兼容 JSON 的输出。同一个 Run 不得并发推进。

## Provider 与 Run 生命周期

### Provider 组合

| Case Provider | Judge Provider | 行为 |
|---|---|---|
| 省略 | 省略 | 官方 Case 与 Judge；需要 API Key |
| 省略 | 自定义 | 官方 Case 与本地 Judge；需要 API Key |
| 自定义 | 省略 | 本地 Case 与官方 Judge；需要 API Key |
| 自定义 | 自定义 | 完全本地 |
| 任意 | `judge=False` | 最后一次提交后结束，不运行 Judge |

自定义 Case Provider 必须设置 `max_steps`；它是 Run 可接受的步骤数上限，不是要求生成的精确数量。官方模式可省略并采用公开服务上限。Provider 输出进入 Run 前会被归一化并验证；KUMA 会拒绝超限 Case，不会截断。

### Run 状态机

正常流程为 `ready` → `input_delivered` → `submitting`；如有后续 Input 则回到 `ready`，否则进入 `completed`。启用 Judge 后继续执行 `judging` → `report_ready`。

| 状态 | 含义 |
|---|---|
| `ready` | 可以交付下一个 Input |
| `input_delivered` | 当前 Input 等待且只允许一次提交 |
| `submitting` | 正在提交并提交 Evidence 事务 |
| `completed` | Input 已处理完；可运行或重试 Judge |
| `judging` | 同步 Judge 调用正在执行 |
| `report_ready` | 已取得验证后的 `TestReport` |
| `cancelled` | 调用者取消 Run，并释放运行时状态 |
| `failed` | 运行时收尾失败 |

在 `submit()` 前重复调用 `get_input()` 会返回同一 Input。非法顺序会抛出 `InputProtocolError`。不再继续时应调用 `run.cancel()`。

### `create_run()` 参数

[Python API 参考](api-reference.zh-CN.md#create_run)是每个参数的类型、默认值、允许值、副作用、返回值和失败行为的权威说明。`on_failure` 只接受 `continue` 或 `stop`。Python API 保持同步，不支持 `wait=False`。

## Evidence、文件、日志与隐私

每次 `get_input()` 到 `submit()` 是一个 Evidence 事务。只有不可变 Submission 成功加入 History 后才会提交 Evidence；Submission 构造失败不会推进日志 offset 或 Trace 预算。

- Repo metadata 有明确上限，只包含路径、类型、大小和 fingerprint，不包含仓库文件正文。
- 默认文件追踪只记录 hash、大小、mode 和变化类型；仅 `upload_diff=True` 时文本内容才进入 Evidence。
- `submit(..., logs=[...])` 只读取显式指定文件的增量，并要求启用 Evidence 采集。
- `save_local=True` 将结构化记录写入 `.kuma/runs/<run_id>/submissions/`；本地记录不能替代官方提交。
- `CaptureStatus`、`missing`、`dropped_count` 和 `runtime_warnings` 用于呈现采集不完整或降级。

框架无关的运行时元数据遵循 [Runtime Evidence 合同](runtime-evidence.md)。v1 仍仅含哈希；只有官方服务明确协商支持 Agent output 后，才会接收经过敏感检查的 completed Agent 最终输出。Agent output canonical JSON 上限为 4 MiB，单份 Runtime Evidence 为 5 MiB，整个官方 multipart 请求为 8 MiB；均不截断。该文档是 schema 与隐私规则的权威说明。

上传到官方服务前，KUMA 会扫描 output、error、路径、diff、显式日志和自定义 Case 中的敏感内容。API Key 仅用于鉴权，不会加入 Evidence。`allow_sensitive=True` 只是普通 Evidence 的显式覆盖，不能替代隔离与 secret 管理。

已知 OpenAI、OpenAI project 与 Anthropic 的 `sk-` 凭证前缀会在官方上传前
按 `sk_api_key` 拒绝。finding 只包含规则和位置，不包含命中的值；KUMA 不做
熵猜测。

自定义 Case 只包含公开 Input 与约束。不要附带 Rubric：`rubric`、
`private_rubric` 和 `rubric_context` 都会在上传前被拒绝；官方 Judge 直接评估
用户提供的公开 Case。

## OpenTelemetry

OpenTelemetry（OTel）是 Agent 框架和 instrumentation 用来产生 span 的标准可观测性接口。KUMA 只把**同一进程中真实产生**的 span 映射为有界 Evidence；它不会伪造 Agent 行为，也不是 OTel Collector、后端或 Trace UI。

仅在需要 Trace Evidence 时安装可选能力，核心包不强制依赖 OTel：

```bash
python -m pip install "kuma-defuzex[otel]"
```

声明的 `opentelemetry-sdk>=1.30,<2` 范围完整支持 Logs exporter 改名：
KUMA 在 1.30–1.38 使用配套的旧 API 名称，从 1.39 起使用配套的新名称，
不会混用两代符号。若安装版本不提供任一完整组合，导入错误会说明当前版本和支持范围。

`create_run()` 现在按以下优先级工作：

| 当前环境 | Run 行为 | Trace 行为 | 提示 |
| --- | --- | --- | --- |
| 显式传入 `trace_evidence` capture | 正常继续 | 使用显式 capture | 无 |
| 已配置兼容的全局 SDK `TracerProvider` | 正常继续 | 自动复用 | 无 |
| 未安装 OTel 或没有兼容的全局 Provider | 正常继续 | 无 Trace Evidence | `trace_auto_capture_unavailable` |
| 自动附着失败 | 正常继续 | 降级为无 Trace | `trace_auto_attach_failed` |

这些 warning 只表示 Evidence 完整性，记录在 `run.runtime_warnings`，不会阻断 `get_input()`、`submit()` 或 Judge。只安装 extra 不会凭空产生 span；Agent 框架或 instrumentation 还必须配置全局 SDK Provider 并实际发出 span。常见情况下无需任何 KUMA 专属设置：

```python
from opentelemetry import trace

from kuma import create_run

run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
    allow_local=True,
)
tracer = trace.get_tracer("my-agent")

while (test_input := run.get_input()) is not None:
    with tracer.start_as_current_span("agent.solve"):
        output = my_agent(test_input)
    report = run.submit(output)
```

如果应用没有兼容 Provider，继续使用 `run.submit(output)` 即可；需要时可将 `trace_auto_capture_unavailable` 转换成面向用户的非阻断提示。

### 仍然支持显式配置

非全局 Provider 或自定义资源上限继续使用原有显式 API。显式 capture 始终优先于自动发现，KUMA 也永远不会替换或重置全局 Provider：

```python
from opentelemetry.sdk.trace import TracerProvider

from kuma import create_run
from kuma.otel import TraceEvidenceLimits, configure_trace_evidence

provider = TracerProvider()
trace_evidence = configure_trace_evidence(
    provider,
    limits=TraceEvidenceLimits(max_spans=100, max_total_bytes=256_000),
)
run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
    allow_local=True,
    trace_evidence=trace_evidence,
)
```

span 数量、属性、事件、文本和整个 Run 的字节数均有上限。拒绝式 allowlist 会排除 prompt、completion、源码、日志正文、Key 与凭证。显式 `submit(output)` 始终是可移植的回退；只有受支持的 Agent/Workflow span 提供合法最终输出时才能省略 output。自动捕获当前覆盖 span；普通日志仍遵循既有的显式 Submission 日志合同。KUMA 不提供 OTLP receiver、跨进程关联、Trace UI 或存储服务。

## Docker 与运行时安全

官方正式运行默认要求 SDK 与 Agent 位于同一个受控容器。`allow_local=True` 是开发开关，不是沙箱。用户仍需限制 Agent 的文件、命令、网络、资源和 secret 权限。

构建仓库提供的用户流程示例：

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t kuma-user-flow .
```

示例所需的工作区和运行参数见[全栈用户流程指南](../examples/full_stack/README.zh-CN.md)。

## 错误、重试与超时

通过 `KumaError` 捕获稳定 SDK 错误：

```python
from kuma.errors import KumaError

try:
    report = run.judge()
except KumaError as exc:
    print(exc.code, exc.retryable, exc.request_id)
```

常见子类包括 `ConfigurationError`、`AuthenticationError`、`PermissionDeniedError`、`ValidationError`、`SensitiveDataError`、`LimitExceededError`、`InputProtocolError`、`ProviderError`、`KumaTimeoutError`、`ServiceBusyError` 和 `ServiceError`。

`timeout` 限制单次公开 HTTP 尝试；`operation_wait_timeout` 限制完整的官方单 Case 或 Judge operation。POST 重试会复用稳定幂等键；只有服务端声明的瞬态失败才会在 `max_retries` 范围内重试，`ServiceBusyError` 不会自动重试。

官方请求会在 `.kuma/requests/` 保留有界元数据，不保存凭证、请求正文、
Evidence 或 Rubric。进程退出后，可使用 `kuma requests list`、
`kuma requests show <client-request-id>` 与
`kuma requests resume <client-request-id>`（或对应 Python API）。已知 operation
只执行 GET 轮询；若接受响应在本地保存 operation ID 前丢失，则通过鉴权查询
恢复。恢复成功的 Judge 报告写入 `.kuma/reports/<run_id>.json`。

## 故障排查

| 现象 | 处理 |
|---|---|
| 缺少 API Key | 配置有效 Key，或使用完全本地 Provider / `judge=False` |
| Agent Profile 被拒绝 | 检查 UTF-8、front matter、必需标题和结构化 Input schema |
| `DockerRequiredError` | 使用同一个受控容器；仅可信开发环境设置 `allow_local=True` |
| `submit()` 返回 `None` | 检查剩余 Input、`judge`、`run.state` 与 `run.history` |
| `input_protocol` | 严格交替执行一次 `get_input()` 与一次 `submit()`，不要并发推进 |
| 敏感数据被拒绝 | 从 output、路径、日志、diff 与自定义 Case 中移除 secret |
| operation 超时或响应丢失 | 查看 `.kuma/requests/`，再恢复同一个客户端请求 ID |
| 缺少 Trace 输出 | 显式提交 JSON 输出，或正确安装并 attach `[otel]` |

## 参考

- [架构](architecture.md)
- [Python API 参考](api-reference.zh-CN.md)
- [策略组](strategy-groups.zh-CN.md)
- [Agent 工具能力](agent-tool-capabilities.zh-CN.md)
- [公开 API Contract](api-contract.md)
- [Runtime Evidence 合同](runtime-evidence.md)
- [最小本地示例](../examples/minimal_local.py)
- [Single Agent 模板](../examples/single_agent_template/README.md)
- [全栈用户流程示例](../examples/full_stack/README.zh-CN.md)
- [安全策略](../SECURITY.md)
- [贡献说明](../CONTRIBUTING.md)
