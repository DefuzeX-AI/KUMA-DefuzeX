# DefuzeX Python SDK guide

This guide holds the detailed setup and integration material intentionally kept out of the project homepage. For the Chinese integration path, see the [中文用户接入指南](../examples/full_stack/USER_GUIDE.md).

## Installation

DefuzeX supports Python 3.10 through 3.14. Install from the current source repository in an isolated environment:

```bash
git clone https://github.com/DefuzeX-AI/KUMA-DefuzeX.git
cd KUMA-DefuzeX
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install .
```

On Linux or macOS:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

Install the optional in-process OpenTelemetry adapter from the checkout only when needed:

```bash
python -m pip install ".[otel]"
```

Contributors should follow [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the editable development install and canonical checks.

## Account-free first run

`defuzex quickstart` runs a deterministic exact-match check in an SDK-owned temporary directory. It reads no user repository and requires no account, API key, Docker, or network:

```bash
defuzex quickstart
```

Use `defuzex quickstart --fail-demo` to inspect the deterministic failure path and non-zero exit status. [`examples/minimal_local.py`](../examples/minimal_local.py) demonstrates a complete local `Run` with a custom Case Provider and no Judge:

```bash
python examples/minimal_local.py
```

## Official service setup

Official Case or Judge providers require a DefuzeX API key beginning with `dfx_`. Supply it through the process environment or the local user credential store; never place it in source, Notebook output, or Git.

Windows PowerShell:

```powershell
$env:DEFUZEX_API_KEY = "dfx_your_key_here"
defuzex whoami
```

Linux or macOS:

```bash
export DEFUZEX_API_KEY="dfx_your_key_here"
defuzex whoami
```

The SDK can validate and atomically save the key without contacting the network:

```python
from defuzex import configure

credential_path = configure(api_key="dfx_your_key_here")
print(credential_path)
```

Credential resolution order is an explicit function argument, `DEFUZEX_API_KEY`, then the user credential file.

### Requirement file

The official Case Provider requires an explicit UTF-8 requirement file with YAML front matter and three sections:

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

The official service currently accepts text Inputs. Custom Case Providers can use locally validated structured Inputs.

### Agent integration

The user owns the Agent invocation; the SDK owns the Run protocol. Replace the deterministic body below with the existing Agent call:

```python
from typing import Any

from defuzex import create_run


def execute_agent(test_input: Any) -> dict[str, Any]:
    return {"result": str(test_input)}


run = create_run(
    repo_path=".",
    requirement_path="requirement.md",
    allow_local=True,  # Trusted local development only.
)

report = None
while (test_input := run.get_input()) is not None:
    report = run.submit(execute_agent(test_input))

print(run.state)
print(report)
```

Production runs should place the SDK and Agent in the same controlled container and omit `allow_local=True`. The [Single Agent template](../examples/single_agent_template/README.md) provides a framework-neutral adapter with explicit timeout and failure handling.

## Run lifecycle and providers

One `Run` follows a strict synchronous sequence:

1. `create_run()` validates configuration and obtains one Case.
2. `get_input()` delivers the current Input.
3. The user invokes the Agent.
4. `submit()` records the result and Evidence atomically.
5. Steps 2–4 repeat until the Case ends; an enabled Judge then returns a `TestReport`.

Do not advance one Run concurrently. Call `run.cancel()` when abandoning it early.

| Case provider | Judge provider | Behavior |
|---|---|---|
| omitted | omitted | Official Case and Judge; API key required |
| omitted | custom | Official Case with local Judge; API key required |
| custom | omitted | Local Case with official Judge; API key required |
| custom | custom | Fully local; the Case carries its public evaluation rule |
| any | `judge=False` | Complete after the final submission without a Judge |

Common `create_run()` controls include:

| Option | Purpose |
|---|---|
| `repo_path`, `requirement_path` | Select the evaluated repository and requirement |
| `case_provider`, `judge_provider` | Replace either official boundary with a custom Provider |
| `max_inputs` | Bound custom Case input count |
| `allow_local` | Explicitly permit trusted local development outside Docker |
| `track_files`, `upload_diff`, `save_local` | Control file Evidence and local step records |
| `timeout`, `operation_wait_timeout`, `max_retries` | Bound public HTTP attempts and official operation polling |
| `trace_evidence` | Attach the optional in-process Trace capture |

The Python API remains synchronous while official single-Case and Judge requests use bounded server operations internally. See [SDK architecture](architecture.md#create_run-编排与调用流程) for the complete sequence and [public API contract](api-contract.md) for the HTTP boundary.

## Evidence and OpenTelemetry

Each `get_input()` to `submit()` interval is one Evidence transaction. File tracking records metadata by default; `logs=[...]` reads only explicitly named file increments. Capture status, missing reasons, dropped counts, and runtime warnings expose incomplete or degraded capture.

OpenTelemetry support is optional and stays in process. DefuzeX adds a standard processor to the provided `TracerProvider`; it does not replace the global provider:

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

Only ended spans associated with the current Input are captured. A restrictive allowlist, size limits, and sensitive-data checks apply. Explicit `submit(output)` remains the portable fallback when Agent instrumentation does not expose a supported final output. The SDK does not provide an OTLP receiver, cross-process correlation, trace UI, or trace storage. See [OpenTelemetry architecture](architecture.md#opentelemetry-适配) for mapping and transaction details.

## Docker and runtime safety

`allow_local=True` is a development switch, not a sandbox. The user remains responsible for the Agent's file, command, network, and secret permissions. Evidence scanning complements container isolation and least privilege; it does not replace them.

The public user-flow image is a build-validation example:

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t defuzex-user-flow .
```

The [Docker user-flow guide](../examples/full_stack/USER_GUIDE.md) and [Notebook](../examples/full_stack/defuzex_v4_real_user_flow.ipynb) describe the full user-owned Agent path and its prerequisites.

## Troubleshooting

Catch stable public errors through `DefuzeError`:

```python
from defuzex.errors import DefuzeError

try:
    report = run.judge()
except DefuzeError as exc:
    print(exc.code, exc.retryable, exc.request_id)
```

| Symptom | Action |
|---|---|
| Missing API key | Use fully local Providers or `judge=False`, or configure a valid key |
| `DockerRequiredError` | Run SDK and Agent in one container; use `allow_local=True` only for trusted development |
| `submit()` returns `None` | Check whether more Inputs remain, whether Judge is disabled, and inspect `run.state` |
| Operation timeout | Preserve the original Run, inspect `retryable`, and retry without changing protocols |
| Missing OTel output | Submit an explicit JSON-compatible output or install and attach `[otel]` correctly |
| Sensitive-data rejection | Remove credentials or sensitive content from output, paths, logs, and uploaded diffs |

Only server-declared transient failures are retried within `max_retries`. Public server internals are not exposed through SDK exceptions.

## Reference

- [SDK architecture](architecture.md)
- [Public API contract](api-contract.md)
- [Chinese integration guide](../examples/full_stack/USER_GUIDE.md)
- [Contributing](../CONTRIBUTING.md)
- [Security policy](../SECURITY.md)
