<p align="center">
  <img src="docs/assets/defuzex-banner.svg" width="760" alt="DefuzeX Python SDK geometric wordmark banner">
</p>

<h1 align="center">DefuzeX Python SDK</h1>

<p align="center">
  <strong>Evidence-first behavior testing for AI agents</strong><br>
  面向 AI Agent 的证据优先行为测试 SDK
</p>

<p align="center">
  <a href="docs/architecture.md"><strong>阅读文档 / Read the Docs →</strong></a>
</p>

<p align="center">
  <a href="#中文">简体中文</a> &nbsp;|&nbsp; <a href="#english">English</a>
</p>

<p align="center">
  <a href="https://github.com/DefuzeX-company/defuzex-python-sdk/actions/workflows/ci.yml"><img src="https://github.com/DefuzeX-company/defuzex-python-sdk/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI status"></a>
  <img src="https://img.shields.io/badge/Python-3.10--3.14-3776AB?logo=python&amp;logoColor=white" alt="Python 3.10 to 3.14">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache--2.0-4C8CBF" alt="Apache 2.0 license"></a>
  <img src="https://img.shields.io/badge/package-v4.0.0-6C63FF" alt="Package v4.0.0">
</p>

---

## 中文

DefuzeX Python SDK 是面向 Agent 行为测试的公开 Python 客户端。它解析测试要求、驱动同步 `Run` 协议、采集有界 Evidence，并通过 DefuzeX 公开 HTTPS API 获取官方 Case 和最终 Judgment。

### 定位与边界

```text
SDK -> DefuzeX public API -> DefuzeX managed services
```

SDK 只实现客户端协议和 Evidence 边界。它不启动或托管 Agent，不选择 Agent 所用模型，不提供沙箱、服务端部署、数据库或后台任务，也不直连 DefuzeX 内部服务。服务端专属的评估材料、执行配置和凭据不会进入 SDK。

详细设计见[架构文档](docs/architecture.md)，HTTP 边界见[公开 API Contract](docs/api-contract.md)。

### 核心概念

- **Case**：一次完整测试的公开输入序列。
- **Input**：当前交给用户 Agent 的一个任务。
- **Run**：严格执行 `get_input()` -> Agent -> `submit()` 的单 Case 状态机。
- **Submission / History**：Agent 对每个 Input 的结果和不可变历史。
- **Evidence**：文件变化、显式日志和可选的同进程 OpenTelemetry spans。
- **Judgment / TestReport**：Judge 对完整 Run 返回的公开结果。
- **Provider**：Case 或 Judge 的边界实现；可以使用官方服务，也可以完全本地自定义。

### 安装

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/DefuzeX-company/defuzex-python-sdk.git
cd defuzex-python-sdk
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Linux/macOS：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

如果 `defuzex` 已在你使用的包源发布，也可以运行 `python -m pip install defuzex`。可选能力按需安装；核心包不强制引入 OpenTelemetry：

```bash
python -m pip install "defuzex[otel]"
python -m pip install -e ".[dev]"
```

仓库版本号不代表同版本已经发布到公共包索引，请以实际包源或所用 Git commit 为准。

### 无账号首次运行

```bash
defuzex quickstart
```

该命令不需要账号、API Key、Docker 或网络，也不会读取当前目录或用户仓库。它在临时目录中使用固定 Input、确定性示例 Agent 和一条公开规则完成本地检查。

```text
Local check: PASS
Score: 100/100
Reason: Output exactly matched the published rule.
Artifact: <temporary directory>/result.json
```

`defuzex quickstart --fail-demo` 会演示确定性失败并返回非零退出码。另一个无需凭据的完整 Run 示例是：

```bash
python examples/minimal_local.py
```

它使用临时仓库、自定义 Case Provider、`judge=False` 和 `allow_local=True`，不会修改用户仓库。

### 官方服务 Quickstart

官方 Case 或官方 Judge 需要 `dfx_` 开头的 API Key。请通过进程环境或本机凭证存储提供，不要写入源码、Notebook 输出或 Git。

```powershell
$env:DEFUZEX_API_KEY = "dfx_your_key_here"
defuzex whoami
```

```bash
export DEFUZEX_API_KEY="dfx_your_key_here"
defuzex whoami
```

也可以让 SDK 原子写入当前用户的凭证目录；此操作不访问网络：

```python
from defuzex import configure

credential_path = configure(api_key="dfx_your_key_here")
print(credential_path)
```

凭证解析优先级为：函数参数、`DEFUZEX_API_KEY`、用户凭证文件。

官方 Case Provider 需要显式的 UTF-8 requirement 文件，例如 `requirement.md`：

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

当前官方服务接受文本 Input。结构化 Input 需要自定义 Case Provider 和本地 JSON Schema。用户负责调用真实 Agent；SDK 只负责 Run 协议：

```python
from typing import Any

from defuzex import create_run


def execute_agent(test_input: Any) -> dict[str, Any]:
    """Replace this body with the Agent invocation being evaluated."""
    return {"result": str(test_input)}


run = create_run(
    repo_path=".",
    requirement_path="requirement.md",
    allow_local=True,  # Local development only; production defaults to Docker.
)

while (test_input := run.get_input()) is not None:
    report = run.submit(execute_agent(test_input))

print(run.state)
print(run.report)
```

`get_input()` 默认返回 JSON 兼容 payload；`get_input(full=True)` 返回不可变的 `DefuzeXInput`。`submit(output)` 要求有限值、可序列化为 JSON 的真实 Agent 结果。

### 公开 API 与配置

| 导入位置 | 用途 |
|---|---|
| `defuzex.configure` | 验证并原子保存 API Key，不访问网络 |
| `defuzex.create_run` | 创建同步 Python `Run` |
| `defuzex.DefuzeClient` | 读取公开 entitlements、strategy catalog 和 Judge 配置 |
| `defuzex.Case`、`DefuzeXInput`、`Submission`、`HistoryItem`、`TestReport` | 不可变公开协议对象 |
| `defuzex.errors` | `DefuzeError` 及稳定错误子类 |
| `defuzex.providers` | 官方/自定义 Provider 协议、context 和 normalizer |
| `defuzex.otel` | 可选的同进程 Trace Evidence attach API |

`Run` 提供 `get_input()`、`submit()`、`judge()`、`cancel()`，以及 `state`、`history`、`report` 和 `runtime_warnings`。

| `create_run()` 参数 | 默认值 | 作用 |
|---|---:|---|
| `repo_path` | `"."` | 被测仓库目录 |
| `requirement_path` | `None` | requirement 文件；官方 Provider 要求显式提供 |
| `case_provider` / `judge_provider` | `None` | 省略时使用相应官方 Provider |
| `strategy` | `"auto"` | 自动选择或显式 strategy ID；实际选择由服务端返回 |
| `max_inputs` | `None` | 自定义 Case 必填的 Input 上限 |
| `judge` / `on_failure` | `True` / `"continue"` | 是否 Judge，以及失败后继续或停止 |
| `allow_local` | `False` | 显式允许非 Docker 的开发运行 |
| `track_files` / `upload_diff` | `True` / `False` | 文件元数据采集与可选文本 diff |
| `save_local` | `False` | 保存步骤记录到 `.defuzex/runs/<run_id>/submissions/` |
| `allow_sensitive` | `False` | 普通 Evidence 的显式扫描覆盖；不影响 OTel allowlist |
| `timeout` | `300.0` | 单次公开 HTTP 请求超时（秒） |
| `operation_wait_timeout` | `600.0` | 官方单 Case/Judge operation 总等待上限（秒） |
| `max_retries` | `2` | 0–5；仅重试服务端标记为瞬态的失败 |
| `api_key` / `trace_evidence` | `None` | 本次凭证与可选 Trace capture |

环境变量包括 `DEFUZEX_API_KEY`、`DEFUZEX_BASE_URL` 和 `DEFUZEX_CONFIG_HOME`。默认公开地址是 `https://defuzex.ai/api/agentdefuze`。非 loopback 地址必须使用 HTTPS，URL 不能包含 credentials。SDK 不接受内部服务地址或数据库连接配置。

### Provider 与 Run 生命周期

| Case | Judge | 行为 |
|---|---|---|
| 省略 | 省略 | 官方 Case + 官方 Judge，需要 API Key |
| 省略 | 自定义 | 官方 Case + 本地 Judge，需要 API Key |
| 自定义 | 省略 | 本地 Case + 官方 Judge，需要 API Key |
| 自定义 | 自定义 | 完全本地；Case 必须包含固定公开 rubric |
| 任意 | `judge=False` | 最后一次提交后结束，不创建 Judge Provider |

自定义 Provider 可以实现公开 Protocol，也可以传 callable。最小实现见 [`examples/minimal_local.py`](examples/minimal_local.py)。所有外部 Case 和 Judgment 都会在进入 Run 前归一化并严格验证。

Run 依次执行：`create_run()` 验证并返回 `ready`；`get_input()` 交付当前 Input；用户 Agent 执行；`submit()` 原子记录 Evidence 和 History；仍有 Input 时回到 `ready`，最后一步进入 `completed`；启用 Judge 时，成功后进入 `report_ready`。

官方 Provider 内部使用 v2 operation 有界轮询，但 Python API 表面保持同步。SDK 不创建后台 worker，`wait=False` 不受支持。operation 超时会保留有限恢复元数据并抛出可重试的 `DefuzeTimeoutError(code="operation_wait_timeout")`。当前高层 API 不能在进程丢失整个 `Run` 对象后仅凭 `run_id` 重建；Judge 重试需要原 Run 和 History。

非法顺序会抛出 `InputProtocolError`。提前放弃时调用 `run.cancel()`，以释放运行锁并隔离迟到 span。每个容器只能有一个 active Run，同一个 Run 只能有一个当前 Input。

### Evidence 与 OpenTelemetry

每次 `get_input()` 到 `submit()` 是一个 Evidence 事务：Repo metadata 只包含相对路径、类型、大小和 fingerprint；文件追踪默认只记录 hash/size/mode 和变化类型；`logs=[...]` 只读取显式文件增量；提交成功后才推进日志 offset 与 Trace 预算。`CaptureStatus`、`missing`、`dropped_count` 和 `runtime_warnings` 表示不完整或降级。`save_local=True` 原子保存结构化步骤记录，但不替代服务端提交。

OpenTelemetry 是可选能力。DefuzeX 使用标准 `SpanProcessor`/`SpanExporter` 扩展点，不替换已有 `TracerProvider`：

```python
from opentelemetry.sdk.trace import TracerProvider

from defuzex import create_run
from defuzex.otel import TraceEvidenceLimits, configure_trace_evidence

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

Capture 只接受同一进程、当前 Input 生命周期内的已结束 spans，并保留父子关系、时间、状态和受控 metadata。属性、事件、span 数量和每 Run 总字节均有硬上限。截断、丢弃和 exporter/flush 失败会进入 capture 状态与 warning，不会伪造成功或破坏 Run。

只有约定的 Agent/Workflow span 最终输出可作为省略 `submit(output)` 时的来源；显式 output 始终优先。没有有效输出时 SDK 会安全失败并要求显式提交。`allow_sensitive=True` 不能放宽 OTel 的拒绝式 allowlist。OTel 不提供跨进程 OTLP receiver、UI 或存储平台。

### Agent、Docker 与安全边界

- 用户代码负责启动 Agent、调用工具、限制权限并返回真实 JSON 结果。
- 正式模式默认要求 SDK 与 Agent 在同一个 Docker 容器；`allow_local=True` 只用于显式本地开发，不是沙箱。
- SDK 的 Evidence 扫描不能替代容器隔离、最小权限或用户对 Agent 网络/文件访问的限制。
- SDK 只调用公开 HTTPS API；上传前扫描 output、error、显式日志、diff、自定义 Case 和 repo path。
- API Key 只进入 `Authorization` header，不进入 body、Repo metadata 或 Evidence。
- SDK 不拥有服务端鉴权策略、scope、配额、计费、服务端评估逻辑、模型执行或数据库。

公开用户镜像只做构建验证：

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t defuzex-user-flow .
```

### 错误处理与故障排查

```python
from defuzex.errors import DefuzeError

try:
    report = run.judge()
except DefuzeError as exc:
    print(exc.code, exc.retryable, exc.request_id)
```

常用类型包括 `ConfigurationError`、`AuthenticationError`、`PermissionDeniedError`、`ValidationError`、`SensitiveDataError`、`LimitExceededError`、`InputProtocolError`、`ProviderError`、`DefuzeTimeoutError`、`ServiceBusyError` 和 `ServiceError`。服务端内部详情不会进入公开异常文本。

| 现象 | 处理 |
|---|---|
| 没有 API Key | 使用自定义 Case 与自定义 Judge，或 `judge=False` |
| `DockerRequiredError` | 生产环境使用同容器；仅本地开发使用 `allow_local=True` |
| `submit()` 返回 `None` | 检查是否仍有 Input、是否关闭 Judge，以及 `state`/`history` |
| operation 超时 | 检查 `retryable`，保留原 Run 后重试，不要切换协议 |
| OTel 未安装或无最终输出 | 安装 `[otel]` 并显式 attach，或调用 `run.submit(output)` |

POST 使用稳定幂等键。只有明确的瞬态失败会在 `max_retries` 范围内退避重试；`ServiceBusyError` 不自动重试。公开响应超过 8 MiB 会在解析前被拒绝。

### 文档、开发与限制

- [SDK 架构](docs/architecture.md)
- [公开 API Contract](docs/api-contract.md)
- [离线最小示例](examples/minimal_local.py)
- [Single Agent Template](examples/single_agent_template/README.md)
- [用户接入指南](examples/full_stack/USER_GUIDE.md)
- [真实用户流程 Notebook](examples/full_stack/defuzex_v4_real_user_flow.ipynb)
- [贡献说明](CONTRIBUTING.md)、[安全报告](SECURITY.md)、[Apache License 2.0](LICENSE)

```text
src/defuzex/                       Python 包与公开入口
src/defuzex/providers/             official/custom Provider 边界
src/defuzex/tracking/              snapshot、diff、log 与 Evidence 事务
docs/                              架构与公开 HTTP contract
examples/minimal_local.py          无凭据离线示例
examples/single_agent_template/    框架无关 Agent 接入模板
examples/full_stack/               用户侧 Docker、Notebook 与接入指南
```

```bash
python -m pip install -e ".[dev]"
python -m ruff check --exclude "*.ipynb" .
python -m ruff format --check --exclude "*.ipynb" .
python -m compileall -q src examples
defuzex quickstart
python examples/minimal_local.py
python -m build
python -m twine check dist/*
```

公开 CI 验证 lint、跨 Python 安装/导入、CLI、离线示例和打包。维护者在发布前另行执行私有安全与跨服务契约回归；公开分发仓不包含内部验收资产。

当前限制：Python Run API 为同步表面；官方单 Case/Judge 内部使用有界 operation 轮询；官方 Case 当前只返回文本 Input；正式 Run 默认要求同容器；每个容器一个 active Run；OTel 仅同进程；SDK 不提供 Agent runner、托管服务部署、UI、模型调用或数据库。

## English

The DefuzeX Python SDK is the public Python client for Agent behavior testing. It parses test requirements, drives a synchronous `Run` protocol, captures bounded Evidence, and obtains official Cases and final Judgments through the DefuzeX public HTTPS API.

### Scope and boundaries

```text
SDK -> DefuzeX public API -> DefuzeX managed services
```

The SDK implements client protocols and Evidence boundaries only. It does not start or host an Agent, select the Agent's model, provide a sandbox, deploy services, own a database, run background jobs, or connect directly to DefuzeX internal services. Server-only evaluation material, execution configuration, and credentials do not enter the SDK.

See the [architecture document](docs/architecture.md) and [public API Contract](docs/api-contract.md).

### Core concepts

- **Case**: the public sequence of inputs for one complete test.
- **Input**: one task currently delivered to the user's Agent.
- **Run**: a single-Case state machine with a strict `get_input()` -> Agent -> `submit()` handshake.
- **Submission / History**: the Agent result for each Input and its immutable history.
- **Evidence**: file changes, explicit logs, and optional in-process OpenTelemetry spans.
- **Judgment / TestReport**: the public result for the complete Run.
- **Provider**: an official or fully local Case/Judge boundary implementation.

### Installation

Python 3.10 or newer is required.

```bash
git clone https://github.com/DefuzeX-company/defuzex-python-sdk.git
cd defuzex-python-sdk
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Linux/macOS:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

If `defuzex` is available from your package index, use `python -m pip install defuzex`. Install optional capabilities only when needed; the core package does not require OpenTelemetry:

```bash
python -m pip install "defuzex[otel]"
python -m pip install -e ".[dev]"
```

A repository version does not prove that the same version is published to a public package index. Use the package index or Git commit you installed as the source of truth.

### Account-free first run

```bash
defuzex quickstart
```

This command needs no account, API Key, Docker, or network and does not read the current directory or a user repository. It uses an isolated temporary directory, fixed Input, deterministic example Agent, and one published rule.

```text
Local check: PASS
Score: 100/100
Reason: Output exactly matched the published rule.
Artifact: <temporary directory>/result.json
```

`defuzex quickstart --fail-demo` demonstrates a deterministic failure and exits non-zero. A complete credential-free Run is also available:

```bash
python examples/minimal_local.py
```

It uses a temporary repository, custom Case Provider, `judge=False`, and `allow_local=True`, and does not modify the user's repository.

### Hosted-service quickstart

An official Case or Judge requires an API Key beginning with `dfx_`. Supply it through the process environment or local user credential store. Never place it in source code, Notebook output, or Git.

```powershell
$env:DEFUZEX_API_KEY = "dfx_your_key_here"
defuzex whoami
```

```bash
export DEFUZEX_API_KEY="dfx_your_key_here"
defuzex whoami
```

The SDK can write the key atomically to the user credential directory without making a network request:

```python
from defuzex import configure

credential_path = configure(api_key="dfx_your_key_here")
print(credential_path)
```

Credential precedence is: function argument, `DEFUZEX_API_KEY`, then the user credential file.

The official Case Provider requires an explicit UTF-8 requirement file, for example `requirement.md`:

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

The hosted service currently accepts text Inputs. Structured Inputs require a custom Case Provider and local JSON Schema. The user owns the real Agent invocation; the SDK owns only the Run protocol:

```python
from typing import Any

from defuzex import create_run


def execute_agent(test_input: Any) -> dict[str, Any]:
    """Replace this body with the Agent invocation being evaluated."""
    return {"result": str(test_input)}


run = create_run(
    repo_path=".",
    requirement_path="requirement.md",
    allow_local=True,  # Local development only; production defaults to Docker.
)

while (test_input := run.get_input()) is not None:
    report = run.submit(execute_agent(test_input))

print(run.state)
print(run.report)
```

`get_input()` returns the JSON-compatible payload by default. `get_input(full=True)` returns an immutable `DefuzeXInput`. `submit(output)` requires the real Agent result as finite JSON-serializable data.

### Public API and configuration

| Import | Purpose |
|---|---|
| `defuzex.configure` | Validate and atomically save an API Key without network access |
| `defuzex.create_run` | Create a synchronous Python `Run` |
| `defuzex.DefuzeClient` | Read public entitlements, strategy catalog, and Judge configuration |
| `defuzex.Case`, `DefuzeXInput`, `Submission`, `HistoryItem`, `TestReport` | Immutable public contract objects |
| `defuzex.errors` | `DefuzeError` and stable subclasses |
| `defuzex.providers` | Official/custom Provider protocols, contexts, and normalizers |
| `defuzex.otel` | Optional in-process Trace Evidence attach API |

`Run` exposes `get_input()`, `submit()`, `judge()`, `cancel()`, and the `state`, `history`, `report`, and `runtime_warnings` properties.

| `create_run()` argument | Default | Purpose |
|---|---:|---|
| `repo_path` | `"."` | Repository under test |
| `requirement_path` | `None` | Requirement file; official Providers require it explicitly |
| `case_provider` / `judge_provider` | `None` | Omit to use the corresponding official Provider |
| `strategy` | `"auto"` | Automatic selection or explicit strategy ID; the service returns the actual selection |
| `max_inputs` | `None` | Required Input bound for a custom Case |
| `judge` / `on_failure` | `True` / `"continue"` | Whether to Judge and whether to continue or stop after failure |
| `allow_local` | `False` | Explicitly permit a non-Docker development run |
| `track_files` / `upload_diff` | `True` / `False` | File metadata capture and optional text diff |
| `save_local` | `False` | Save step records under `.defuzex/runs/<run_id>/submissions/` |
| `allow_sensitive` | `False` | Explicit ordinary-Evidence scan override; does not affect the OTel allowlist |
| `timeout` | `300.0` | Per-request public HTTP timeout in seconds |
| `operation_wait_timeout` | `600.0` | Total wait bound for one official Case/Judge operation |
| `max_retries` | `2` | 0–5; retry only service-marked transient failures |
| `api_key` / `trace_evidence` | `None` | Per-call credential and optional Trace capture |

Environment variables are `DEFUZEX_API_KEY`, `DEFUZEX_BASE_URL`, and `DEFUZEX_CONFIG_HOME`. The default public URL is `https://defuzex.ai/api/agentdefuze`. Non-loopback URLs must use HTTPS and URLs cannot contain credentials. The SDK does not accept internal service addresses or database connection configuration.

### Providers and Run lifecycle

| Case | Judge | Behavior |
|---|---|---|
| Omitted | Omitted | Official Case + official Judge; API Key required |
| Omitted | Custom | Official Case + local Judge; API Key required |
| Custom | Omitted | Local Case + official Judge; API Key required |
| Custom | Custom | Fully local; Case must contain a fixed public rubric |
| Any | `judge=False` | Finish after the final submission without a Judge Provider |

A custom Provider may implement the public Protocol or be a callable. See [`examples/minimal_local.py`](examples/minimal_local.py). Every external Case and Judgment is normalized and strictly validated before entering a Run.

The lifecycle is: `create_run()` validates and returns `ready`; `get_input()` delivers the current Input; the user Agent runs; `submit()` atomically commits Evidence and History; the Run returns to `ready` when more Inputs remain and enters `completed` after the last; an enabled successful Judge moves it to `report_ready`.

Official Providers use bounded v2 operation polling internally while the Python API stays synchronous. The SDK creates no background worker and `wait=False` is unsupported. A deadline preserves bounded resume metadata and raises retryable `DefuzeTimeoutError(code="operation_wait_timeout")`. The high-level API cannot reconstruct a lost `Run` from only `run_id` after the process exits; Judge retry needs the original Run and History.

Invalid ordering raises `InputProtocolError`. Call `run.cancel()` when abandoning a Run to release its lock and isolate late spans. One container may have only one active Run, and one Run may have only one current Input.

### Evidence and OpenTelemetry

Each `get_input()` to `submit()` interval is an Evidence transaction. Repository metadata contains only relative paths, types, sizes, and a fingerprint. File tracking records hashes/sizes/modes and change types by default. `logs=[...]` reads only increments from explicit files. Log offsets and Trace budgets advance only after commit. `CaptureStatus`, `missing`, `dropped_count`, and `runtime_warnings` expose incomplete capture. `save_local=True` atomically stores structured step records but does not replace hosted submission.

OpenTelemetry is optional. DefuzeX uses standard `SpanProcessor`/`SpanExporter` extension points and does not replace an existing `TracerProvider`:

```python
from opentelemetry.sdk.trace import TracerProvider

from defuzex import create_run
from defuzex.otel import TraceEvidenceLimits, configure_trace_evidence

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

Capture accepts only ended spans from the same process and current Input lifecycle. It preserves parentage, timing, status, and controlled metadata. Attributes, events, span counts, and total bytes per Run have hard limits. Truncation, drops, and exporter/flush failures appear in status and warnings without fabricating success or breaking the Run.

Only a convention-compliant Agent/Workflow span final output may supply an omitted `submit(output)` value; explicit output always wins. Without a valid output, the SDK fails safely and requests an explicit submission. `allow_sensitive=True` cannot relax the OTel deny-by-default allowlist. OTel support is not a cross-process OTLP receiver, UI, or storage platform.

### Agent, Docker, and security boundaries

- User code starts the Agent, invokes tools, constrains permissions, and returns the real JSON result.
- Production mode requires the SDK and Agent in one Docker container by default. `allow_local=True` is an explicit development escape hatch, not a sandbox.
- Evidence scanning does not replace container isolation, least privilege, or user control of Agent network/filesystem access.
- The SDK calls only the public HTTPS API and scans output, errors, explicit logs, diffs, custom Cases, and repository paths before upload.
- The API Key enters only the `Authorization` header, never the body, repository metadata, or Evidence.
- The SDK does not own hosted authentication policy, scopes, quotas, billing, server evaluation logic, model execution, or databases.

The public user image is build-validated only:

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t defuzex-user-flow .
```

### Errors and troubleshooting

```python
from defuzex.errors import DefuzeError

try:
    report = run.judge()
except DefuzeError as exc:
    print(exc.code, exc.retryable, exc.request_id)
```

Common types include `ConfigurationError`, `AuthenticationError`, `PermissionDeniedError`, `ValidationError`, `SensitiveDataError`, `LimitExceededError`, `InputProtocolError`, `ProviderError`, `DefuzeTimeoutError`, `ServiceBusyError`, and `ServiceError`. Internal service details do not enter public exception text.

| Symptom | Action |
|---|---|
| No API Key | Use a custom Case with a custom Judge, or `judge=False` |
| `DockerRequiredError` | Use one container in production; use `allow_local=True` only for explicit local development |
| `submit()` returns `None` | Check for remaining Inputs, disabled Judge, and `state`/`history` |
| Operation timeout | Inspect `retryable`, retain the original Run, and retry without switching protocols |
| OTel missing or no final output | Install `[otel]` and attach explicitly, or call `run.submit(output)` |

POST requests use stable idempotency keys. Only explicitly transient failures are retried with bounded backoff under `max_retries`; `ServiceBusyError` is not retried automatically. Public responses larger than 8 MiB are rejected before parsing.

### Documentation, development, and limitations

- [SDK architecture](docs/architecture.md)
- [Public API Contract](docs/api-contract.md)
- [Offline minimal example](examples/minimal_local.py)
- [Single Agent Template](examples/single_agent_template/README.md)
- [User integration guide](examples/full_stack/USER_GUIDE.md)
- [Real user-flow Notebook](examples/full_stack/defuzex_v4_real_user_flow.ipynb)
- [Contributing](CONTRIBUTING.md), [security reporting](SECURITY.md), and [Apache License 2.0](LICENSE)

```text
src/defuzex/                       Python package and public entry points
src/defuzex/providers/             Official/custom Provider boundaries
src/defuzex/tracking/              Snapshot, diff, log, and Evidence transactions
docs/                              Architecture and public HTTP contract
examples/minimal_local.py          Credential-free offline example
examples/single_agent_template/    Framework-neutral Agent integration template
examples/full_stack/               User-side Docker, Notebook, and integration guide
```

```bash
python -m pip install -e ".[dev]"
python -m ruff check --exclude "*.ipynb" .
python -m ruff format --check --exclude "*.ipynb" .
python -m compileall -q src examples
defuzex quickstart
python examples/minimal_local.py
python -m build
python -m twine check dist/*
```

Public CI checks lint, cross-Python installation/import, the CLI, offline examples, and packaging. Maintainers run separate private security and cross-service contract regressions before release; the public distribution does not contain internal acceptance assets.

Current limitations: the Python Run API is synchronous; official single-Case/Judge calls use bounded operation polling internally; official Cases currently contain text Inputs only; production Runs require one shared container by default; each container has one active Run; OTel capture is in-process only; and the SDK does not provide an Agent runner, managed-service deployment, UI, model execution, or database.
