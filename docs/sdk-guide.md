# KUMA Python SDK guide

[English](sdk-guide.md) | [简体中文](sdk-guide.zh-CN.md)

This is the canonical user guide for KUMA configuration and integration. The package, CLI, and environment variables use `kuma` / `KUMA_*`; versioned `defuzex.*` wire schemas remain unchanged for server compatibility.

## Installation

KUMA supports Python 3.10 through 3.14. Create an isolated environment:

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install "kuma-defuzex==0.1.0"
```

Linux or macOS:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "kuma-defuzex==0.1.0"
```

Optional OpenTelemetry support:

```bash
python -m pip install "kuma-defuzex[otel]==0.1.0"
```

Contributors should use the editable development setup in [`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Local quickstart

The CLI quickstart runs a deterministic exact-match check in an SDK-owned temporary directory. It reads no user repository and requires no account, API key, Docker, or network:

```bash
kuma quickstart
```

Use `kuma quickstart --fail-demo` to exercise the deterministic failure path. A complete local `Run` with a custom Case Provider and no Judge is also available:

```bash
python examples/minimal_local.py
```

## Configuration

### API key

Official Case or Judge Providers require a KUMA API key beginning with `dfx_`. Keep it in the process environment or user credential store; never place it in source, Notebook output, logs, or Git.

Windows PowerShell:

```powershell
$env:KUMA_API_KEY = "dfx_your_key_here"
kuma whoami
```

Linux or macOS:

```bash
export KUMA_API_KEY="dfx_your_key_here"
kuma whoami
```

The SDK can validate and atomically store the key without a network request:

```python
from kuma import configure

credential_path = configure(api_key="dfx_your_key_here")
print(credential_path)
```

Credential precedence is: `create_run(api_key=...)`, `KUMA_API_KEY`, then the user credential file.

| Environment variable | Purpose |
|---|---|
| `KUMA_API_KEY` | Credential for official Providers |
| `KUMA_CONFIG_HOME` | Override the user credential directory |
| `KUMA_BASE_URL` | Override the accepted public or loopback API base URL; non-loopback URLs must use HTTPS |

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

`agent_description`, `input_type`, and all three headings are required. Official Cases currently accept text Inputs. Structured Inputs require a custom Case Provider plus a locally validated JSON Schema declared through `input_schema`.

### Agent integration

The user owns Agent execution; KUMA owns the synchronous `Run` protocol. Replace the deterministic function body with the existing Agent call:

```python
from typing import Any

from kuma import create_run


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

`get_input()` returns the JSON-compatible payload; `get_input(full=True)` returns an immutable `KumaInput`. Completed submissions require finite JSON-compatible output. Do not advance one Run concurrently.

## Providers and Run lifecycle

### Provider combinations

| Case Provider | Judge Provider | Behavior |
|---|---|---|
| omitted | omitted | Official Case and Judge; API key required |
| omitted | custom | Official Case with local Judge; API key required |
| custom | omitted | Local Case with official Judge; API key required |
| custom | custom | Fully local |
| any | `judge=False` | Complete after the final submission without a Judge |

Custom Case Providers require `max_inputs`. Provider outputs are normalized and validated before entering the Run.

### Run state machine

The normal sequence is `ready` → `input_delivered` → `submitting`, returning to `ready` for another Input or moving to `completed`. An enabled Judge uses `judging` → `report_ready`.

| State | Meaning |
|---|---|
| `ready` | The next Input may be delivered |
| `input_delivered` | The current Input awaits exactly one submission |
| `submitting` | The submission and Evidence transaction are committing |
| `completed` | Input processing ended; a Judge may run or retry |
| `judging` | The synchronous Judge call is in progress |
| `report_ready` | A validated `TestReport` is available |
| `cancelled` | The caller cancelled the Run and released runtime state |
| `failed` | Runtime finalization failed |

Repeated `get_input()` calls before `submit()` return the same Input. Invalid ordering raises `InputProtocolError`. Call `run.cancel()` when abandoning a Run.

### `create_run()` parameters

| Parameter | Default | Purpose |
|---|---:|---|
| `repo_path` | `"."` | Repository evaluated by the Agent |
| `requirement_path` | `None` | Requirement file; official Case Providers require it |
| `case_provider` | `None` | Custom Case Provider; omitted selects the official Provider |
| `judge_provider` | `None` | Custom Judge Provider; omitted selects the official Provider when judging |
| `strategy` | `"auto"` | Automatic selection or an explicit strategy ID |
| `max_inputs` | `None` | Positive Input bound; required for custom Cases |
| `judge` | `True` | Run the configured Judge after the final Input |
| `on_failure` | `"continue"` | Continue or stop after a failed submission |
| `allow_local` | `False` | Permit trusted development outside Docker |
| `track_files` | `True` | Capture bounded file metadata around each Input |
| `upload_diff` | `False` | Include bounded text diffs in file Evidence |
| `save_local` | `False` | Save submission records under `.kuma/runs/` |
| `allow_sensitive` | `False` | Explicit override for ordinary Evidence scanning; does not relax Trace allowlists |
| `timeout` | `300.0` | Per-request public HTTP timeout in seconds |
| `operation_wait_timeout` | `600.0` | Total wait bound for one official Case or Judge operation |
| `max_retries` | `2` | Automatic transient retry count, from 0 through 5 |
| `api_key` | `None` | Per-call credential with highest precedence |
| `trace_evidence` | `None` | Capture returned by `configure_trace_evidence()` |

`on_failure` accepts only `continue` or `stop`. The Python API is synchronous; `wait=False` is not supported.

## Evidence, files, logs, and privacy

Each `get_input()` to `submit()` interval is one Evidence transaction. Evidence commits only after the immutable Submission is appended to History; a failed submission build does not advance log offsets or Trace budgets.

- Repository metadata is bounded and contains paths, types, sizes, and a fingerprint—not repository file contents.
- File tracking records hashes, sizes, modes, and change types by default. Text content enters Evidence only when `upload_diff=True`.
- `submit(..., logs=[...])` reads increments only from explicitly selected files and requires Evidence capture to be enabled.
- `save_local=True` writes structured records under `.kuma/runs/<run_id>/submissions/`; local persistence does not replace official submission.
- `CaptureStatus`, `missing`, `dropped_count`, and `runtime_warnings` expose partial or degraded capture.

Framework-neutral runtime metadata follows the canonical, hash-only [Runtime Evidence contract](runtime-evidence.md); that page is the authoritative schema and privacy reference.

Before official upload, KUMA scans output, errors, paths, diffs, explicit logs, and custom Cases for sensitive material. The API key is used for authorization and is not added to Evidence. `allow_sensitive=True` is an explicit ordinary-Evidence override, not a substitute for isolation or secret hygiene.

## OpenTelemetry

The optional adapter captures ended spans from the same process and current Input. It adds a standard processor to the supplied `TracerProvider`; it does not replace the application's provider:

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

Span counts, attributes, events, text, and total Run bytes are bounded. A restrictive allowlist excludes prompts, completions, source, log bodies, keys, and credentials. Explicit `submit(output)` remains the portable fallback; omitted output works only when supported Agent/Workflow spans expose a valid final output. KUMA does not provide an OTLP receiver, cross-process correlation, trace UI, or storage service.

## Docker and runtime security

Official production runs require the SDK and Agent in the same controlled container by default. `allow_local=True` is a development switch, not a sandbox. The user remains responsible for the Agent's file, command, network, resource, and secret permissions.

Build the supplied user-flow example:

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t kuma-user-flow .
```

See the [full-stack example guide](../examples/full_stack/USER_GUIDE.md) for its exact workspace and runtime requirements.

## Errors, retries, and timeouts

Catch stable SDK errors through `KumaError`:

```python
from kuma.errors import KumaError

try:
    report = run.judge()
except KumaError as exc:
    print(exc.code, exc.retryable, exc.request_id)
```

Common subclasses include `ConfigurationError`, `AuthenticationError`, `PermissionDeniedError`, `ValidationError`, `SensitiveDataError`, `LimitExceededError`, `InputProtocolError`, `ProviderError`, `KumaTimeoutError`, `ServiceBusyError`, and `ServiceError`.

`timeout` bounds one public HTTP attempt. `operation_wait_timeout` bounds the complete official single-Case or Judge operation. POST retries reuse a stable idempotency key; only server-declared transient failures are retried within `max_retries`, and `ServiceBusyError` is not retried automatically.

An operation timeout retains bounded recovery metadata without storing credentials, request content, Evidence, or results. Judge retry requires the original Run and History; the high-level API cannot rebuild a lost Run from only `run_id` after process exit.

## Troubleshooting

| Symptom | Action |
|---|---|
| Missing API key | Configure a valid key or use fully local Providers / `judge=False` |
| Requirement rejected | Check UTF-8, front matter, required headings, and structured-input schema |
| `DockerRequiredError` | Use one controlled container; enable `allow_local=True` only for trusted development |
| `submit()` returns `None` | Check remaining Inputs, `judge`, `run.state`, and `run.history` |
| `input_protocol` | Alternate one `get_input()` with one `submit()` and avoid concurrent advancement |
| Sensitive-data rejection | Remove secrets from output, paths, logs, diffs, and custom Cases |
| Operation timeout | Keep the original Run, inspect `retryable`, and retry without changing protocols |
| Missing Trace output | Submit explicit JSON output or install and attach `[otel]` correctly |

## Reference

- [Architecture](architecture.md)
- [Public API contract](api-contract.md)
- [Runtime Evidence contract](runtime-evidence.md)
- [Minimal local example](../examples/minimal_local.py)
- [Single Agent template](../examples/single_agent_template/README.md)
- [Full-stack example](../examples/full_stack/USER_GUIDE.md)
- [Security policy](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
