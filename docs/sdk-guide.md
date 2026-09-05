# KUMA Python SDK guide

Evidence capacity: the default Trace budget is 8 MiB across one Run; span,
attribute and event limits and visible loss counters remain active. Canonical
Agent-output JSON allows 4 MiB, one Runtime Evidence envelope allows 5 MiB, and
the complete multipart body (including metadata/framing) allows 8 MiB. Lower
Backend limits still apply. Output is never truncated. JSON quotes and escapes
count: an ASCII string may contain at most 4,194,302 characters before its two
JSON quotes; Unicode escapes can use more bytes. Run the offline capacity check
with `python tools/verify_evidence_capacity.py` from a source checkout.

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

### Agent Profile file

The official Case Provider requires an explicit UTF-8 Agent Profile file with YAML front matter and three sections:

The Strategy Group remains authoritative for the testing capability, domain, and method. The Agent Profile only supplies context about the Agent, its production scenario, expected behavior, and prohibited boundaries; its prose never selects, replaces, or overrides the group. If the profile omits `strategy_group`, KUMA uses the catalog's exact default group.

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

### Strategy Groups and Agent capabilities

Authenticated users can inspect the current validated public Strategy Group catalog before editing an Agent Profile:

```bash
kuma strategies list
kuma strategies list --output strategy-groups.json
```

Add the selected group `id` and exact `version` through the closed `strategy_group` front-matter object. Omitting it uses the catalog's exact default; an invalid explicit selection or missing Evidence capability fails closed. `scan_strategy_group=True` explicitly enables conservative local suggestion and remains off by default. See [Strategy Groups](strategy-groups.md) for the Agent Profile schema, CLI options, typed Python API, default behavior, and privacy boundary.

An optional `tool_capabilities` relative path can link a reviewed local capability document. Create or validate it with `kuma tools scan` / `kuma tools validate`, or use the equivalent Python helpers. The file is not uploaded; it is a user-controlled claim that may contribute only its closed Evidence capability set to local suggestion. See [Agent tool capabilities](agent-tool-capabilities.md) for its schema, bounds, CLI, Python API, and path rules.

### Agent integration

The user owns Agent execution; KUMA owns the synchronous `Run` protocol. Replace the deterministic function body with the existing Agent call:

```python
from typing import Any

from kuma import create_run


def execute_agent(test_input: Any) -> dict[str, Any]:
    return {"result": str(test_input)}


run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
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

Custom Case Providers require `max_steps`, the maximum number of steps the Run
will accept rather than an exact requested count. Official mode may omit it to
use the public service limit. Provider outputs are normalized and validated;
KUMA rejects an over-limit Case instead of truncating it.

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

The [Python API reference](api-reference.md#create_run) is authoritative for
every argument's type, default, accepted values, side effects, return value, and
failure behavior. `on_failure` accepts only `continue` or `stop`. The Python API
is synchronous and does not support `wait=False`.

## Evidence, files, logs, and privacy

Each `get_input()` to `submit()` interval is one Evidence transaction. Evidence commits only after the immutable Submission is appended to History; a failed submission build does not advance log offsets or Trace budgets.

- Repository metadata is bounded and contains paths, types, sizes, and a fingerprint—not repository file contents.
- File tracking records hashes, sizes, modes, and change types by default. Text content enters Evidence only when `upload_diff=True`.
- `submit(..., logs=[...])` reads increments only from explicitly selected files and requires Evidence capture to be enabled.
- `save_local=True` writes structured records under `.kuma/runs/<run_id>/submissions/`; local persistence does not replace official submission.
- `CaptureStatus`, `missing`, `dropped_count`, and `runtime_warnings` expose partial or degraded capture.

Framework-neutral runtime metadata follows the [Runtime Evidence contract](runtime-evidence.md). v1 remains hash-only; only an explicitly negotiated output-capable official service receives a scanned completed Agent output. Canonical Agent output is capped at 4 MiB, one Runtime Evidence item at 5 MiB, and the complete official multipart body at 8 MiB. Nothing is truncated. That page is the authoritative schema and privacy reference.

Before official upload, KUMA scans output, errors, paths, diffs, explicit logs, and custom Cases for sensitive material. The API key is used for authorization and is not added to Evidence. `allow_sensitive=True` is an explicit ordinary-Evidence override, not a substitute for isolation or secret hygiene.

Known OpenAI, OpenAI project, and Anthropic `sk-` credential prefixes are
blocked as `sk_api_key` before official upload. Findings contain only the rule
and location, never the matched value; KUMA does not use entropy guessing.

Custom Cases contain public Inputs and constraints only. Do not attach a
Rubric: `rubric`, `private_rubric`, and `rubric_context` are rejected before
upload. The official Judge evaluates the supplied public Case directly.

## OpenTelemetry

OpenTelemetry (OTel) is the standard observability API used by Agent frameworks and instrumentation to emit spans. KUMA maps spans that were **actually emitted in the same process** into bounded Evidence. It does not invent Agent activity and is not an OTel Collector, backend, or trace UI.

Install OTel support only when trace capture is needed; the core package does not require it:

```bash
python -m pip install "kuma-defuzex[otel]"
```

The declared `opentelemetry-sdk>=1.30,<2` range is supported across the Logs
exporter rename: KUMA uses the matching old API pair on 1.30–1.38 and the new
pair from 1.39 onward. If an installed release exposes neither complete pair,
the import error reports the installed version and the supported range.

`create_run()` now follows this precedence:

| Environment | Run behavior | Trace behavior | Warning |
| --- | --- | --- | --- |
| Explicit `trace_evidence` capture | Continues | Uses the supplied capture | None |
| Compatible global SDK `TracerProvider` already configured | Continues | Reuses it automatically | None |
| OTel missing or no compatible global provider | Continues | No Trace Evidence | `trace_auto_capture_unavailable` |
| Automatic attachment fails | Continues | Degrades to no Trace Evidence | `trace_auto_attach_failed` |

The warnings are Evidence-completeness signals in `run.runtime_warnings`; they never block `get_input()`, `submit()`, or Judge. Installing the extra alone does not create spans. A framework or instrumentation must configure a global SDK provider and emit spans. In that common case, no KUMA-specific setup is needed:

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

If the application has no compatible provider, continue with `run.submit(output)` and optionally show a user-facing notice when `trace_auto_capture_unavailable` is present.

### Explicit configuration remains supported

Use the existing explicit API for a non-global provider or custom limits. Explicit capture always wins over automatic discovery, and KUMA never replaces or resets a global provider:

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

Span counts, attributes, events, text, and total Run bytes are bounded. A restrictive allowlist excludes prompts, completions, source, log bodies, keys, and credentials. Explicit `submit(output)` remains the portable fallback; omitted output works only when supported Agent/Workflow spans expose a valid final output. Automatic capture currently covers spans; ordinary logs remain governed by the existing explicit Submission log contract. KUMA does not provide an OTLP receiver, cross-process correlation, trace UI, or storage service.

## Docker and runtime security

Official production runs require the SDK and Agent in the same controlled container by default. `allow_local=True` is a development switch, not a sandbox. The user remains responsible for the Agent's file, command, network, resource, and secret permissions.

Build the supplied user-flow example:

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t kuma-user-flow .
```

See the [full-stack user-flow guide](../examples/full_stack/README.md) for its exact workspace and runtime requirements.

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

Official starts retain bounded request metadata under `.kuma/requests/` without
storing credentials, request content, Evidence, or Rubrics. After a process
exit, use `kuma requests list`, `kuma requests show <client-request-id>`, and
`kuma requests resume <client-request-id>` (or the matching Python APIs). Known
operations are polled with GET only; an accepted response lost before the local
operation ID was saved is recovered through authenticated lookup. A recovered
Judge report is written to `.kuma/reports/<run_id>.json`.

## Troubleshooting

| Symptom | Action |
|---|---|
| Missing API key | Configure a valid key or use fully local Providers / `judge=False` |
| Agent Profile rejected | Check UTF-8, front matter, required headings, and structured-input schema |
| `DockerRequiredError` | Use one controlled container; enable `allow_local=True` only for trusted development |
| `submit()` returns `None` | Check remaining Inputs, `judge`, `run.state`, and `run.history` |
| `input_protocol` | Alternate one `get_input()` with one `submit()` and avoid concurrent advancement |
| Sensitive-data rejection | Remove secrets from output, paths, logs, diffs, and custom Cases |
| Operation timeout or lost response | Inspect `.kuma/requests/`, then resume the same client request ID |
| Missing Trace output | Submit explicit JSON output or install and attach `[otel]` correctly |

## Reference

- [Architecture](architecture.md)
- [Python API reference](api-reference.md)
- [Strategy Groups](strategy-groups.md)
- [Agent tool capabilities](agent-tool-capabilities.md)
- [Public API contract](api-contract.md)
- [Runtime Evidence contract](runtime-evidence.md)
- [Minimal local example](../examples/minimal_local.py)
- [Single Agent template](../examples/single_agent_template/README.md)
- [Full-stack user-flow example](../examples/full_stack/README.md)
- [Security policy](../SECURITY.md)
- [Contributing](../CONTRIBUTING.md)
