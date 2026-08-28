# KUMA Python SDK 指南

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
python -m pip install "kuma==0.1.0"
```

Linux 或 macOS：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "kuma==0.1.0"
```

按需安装 OpenTelemetry 能力：

```bash
python -m pip install "kuma[otel]==0.1.0"
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

### Requirement 文件

官方 Case Provider 要求显式提供 UTF-8 requirement 文件，包含 YAML front matter 和三个章节：

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

### 接入 Agent

用户负责执行 Agent，KUMA 负责同步 `Run` 协议。将下面的确定性函数体替换为现有 Agent 调用：

```python
from typing import Any

from kuma import create_run


def execute_agent(test_input: Any) -> dict[str, Any]:
    return {"result": str(test_input)}


run = create_run(
    repo_path=".",
    requirement_path="requirement.md",
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

自定义 Case Provider 必须设置 `max_inputs`。Provider 输出进入 Run 前会被归一化并验证。

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

| 参数 | 默认值 | 用途 |
|---|---:|---|
| `repo_path` | `"."` | Agent 被测仓库 |
| `requirement_path` | `None` | Requirement 文件；官方 Case Provider 必填 |
| `case_provider` | `None` | 自定义 Case Provider；省略时使用官方 Provider |
| `judge_provider` | `None` | 自定义 Judge Provider；启用 Judge 且省略时使用官方 Provider |
| `strategy` | `"auto"` | 自动选择或显式 strategy ID |
| `max_inputs` | `None` | 正数 Input 上限；自定义 Case 必填 |
| `judge` | `True` | 最后一个 Input 后运行已配置的 Judge |
| `on_failure` | `"continue"` | Submission 失败后继续或停止 |
| `allow_local` | `False` | 允许在 Docker 外进行可信开发运行 |
| `track_files` | `True` | 在每个 Input 前后采集有界文件元数据 |
| `upload_diff` | `False` | 在文件 Evidence 中加入有界文本 diff |
| `save_local` | `False` | 将 Submission 记录保存到 `.kuma/runs/` |
| `allow_sensitive` | `False` | 显式覆盖普通 Evidence 扫描；不会放宽 Trace allowlist |
| `timeout` | `300.0` | 单次公开 HTTP 请求超时，单位为秒 |
| `operation_wait_timeout` | `600.0` | 单次官方 Case 或 Judge operation 的总等待上限 |
| `max_retries` | `2` | 瞬态失败自动重试次数，取值 0 至 5 |
| `api_key` | `None` | 优先级最高的本次调用凭证 |
| `trace_evidence` | `None` | `configure_trace_evidence()` 返回的 capture |

`on_failure` 只接受 `continue` 或 `stop`。Python API 保持同步，不支持 `wait=False`。

## Evidence、文件、日志与隐私

每次 `get_input()` 到 `submit()` 是一个 Evidence 事务。只有不可变 Submission 成功加入 History 后才会提交 Evidence；Submission 构造失败不会推进日志 offset 或 Trace 预算。

- Repo metadata 有明确上限，只包含路径、类型、大小和 fingerprint，不包含仓库文件正文。
- 默认文件追踪只记录 hash、大小、mode 和变化类型；仅 `upload_diff=True` 时文本内容才进入 Evidence。
- `submit(..., logs=[...])` 只读取显式指定文件的增量，并要求启用 Evidence 采集。
- `save_local=True` 将结构化记录写入 `.kuma/runs/<run_id>/submissions/`；本地记录不能替代官方提交。
- `CaptureStatus`、`missing`、`dropped_count` 和 `runtime_warnings` 用于呈现采集不完整或降级。

框架无关的运行时元数据遵循规范、仅含哈希的 [Runtime Evidence 合同](runtime-evidence.md)；其 schema 与隐私规则以该文档为准。

上传到官方服务前，KUMA 会扫描 output、error、路径、diff、显式日志和自定义 Case 中的敏感内容。API Key 仅用于鉴权，不会加入 Evidence。`allow_sensitive=True` 只是普通 Evidence 的显式覆盖，不能替代隔离与 secret 管理。

## OpenTelemetry

可选 adapter 只采集同一进程、当前 Input 中已结束的 span。它向用户提供的 `TracerProvider` 添加标准 processor，不会替换应用已有的 provider：

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
    requirement_path="requirement.md",
    allow_local=True,
    trace_evidence=trace_evidence,
)
```

span 数量、属性、事件、文本和整个 Run 的字节数均有上限。拒绝式 allowlist 会排除 prompt、completion、源码、日志正文、Key 与凭证。显式 `submit(output)` 始终是可移植的回退；只有受支持的 Agent/Workflow span 提供合法最终输出时才能省略 output。KUMA 不提供 OTLP receiver、跨进程关联、Trace UI 或存储服务。

## Docker 与运行时安全

官方正式运行默认要求 SDK 与 Agent 位于同一个受控容器。`allow_local=True` 是开发开关，不是沙箱。用户仍需限制 Agent 的文件、命令、网络、资源和 secret 权限。

构建仓库提供的用户流程示例：

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t kuma-user-flow .
```

示例所需的工作区和运行参数见[完整流程示例指南](../examples/full_stack/USER_GUIDE.md)。

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

operation 超时只保留有界恢复元数据，不保存凭证、请求正文、Evidence 或结果。Judge 重试需要保留原 Run 与 History；进程退出并丢失 Run 后，高层 API 不能只通过 `run_id` 重建。

## 故障排查

| 现象 | 处理 |
|---|---|
| 缺少 API Key | 配置有效 Key，或使用完全本地 Provider / `judge=False` |
| Requirement 被拒绝 | 检查 UTF-8、front matter、必需标题和结构化 Input schema |
| `DockerRequiredError` | 使用同一个受控容器；仅可信开发环境设置 `allow_local=True` |
| `submit()` 返回 `None` | 检查剩余 Input、`judge`、`run.state` 与 `run.history` |
| `input_protocol` | 严格交替执行一次 `get_input()` 与一次 `submit()`，不要并发推进 |
| 敏感数据被拒绝 | 从 output、路径、日志、diff 与自定义 Case 中移除 secret |
| operation 超时 | 保留原 Run，检查 `retryable`，不要切换协议后重试 |
| 缺少 Trace 输出 | 显式提交 JSON 输出，或正确安装并 attach `[otel]` |

## 参考

- [架构](architecture.md)
- [公开 API Contract](api-contract.md)
- [Runtime Evidence 合同](runtime-evidence.md)
- [最小本地示例](../examples/minimal_local.py)
- [Single Agent 模板](../examples/single_agent_template/README.md)
- [完整流程示例](../examples/full_stack/USER_GUIDE.md)
- [安全策略](../SECURITY.md)
- [贡献说明](../CONTRIBUTING.md)
