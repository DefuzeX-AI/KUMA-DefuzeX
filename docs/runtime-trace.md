# Runtime Trace sent to Judge

KUMA can send the **actual in-process OTel observations** it captured, not just
a Trace hash. Install the optional `otel` extra and use an existing compatible
Provider (automatic attachment or explicit `configure_trace_evidence`). Once
capture is enabled, captured Trace is included in Official Judge requests when
the server advertises `runtime_trace`; there is no second hidden upload switch.
A server without this capability causes `runtime_evidence_unsupported` before
Judge POST. Runs without Trace capture retain existing compatibility.

## What is captured

An actual span with `gen_ai.operation.name="execute_tool"` may carry
`gen_ai.tool.name`, `gen_ai.tool.type`, `gen_ai.tool.call.id`,
`gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`. Existing instrumentation
must record these values: KUMA does not execute tools, parse stdout, guess from a
span name, or treat a model's proposed tool call as an executed tool.

For example, this instruments a real local function without network or a model:

```python
import json
from opentelemetry.sdk.trace import TracerProvider
from kuma.otel import configure_trace_evidence

provider = TracerProvider()
capture = configure_trace_evidence(provider)
tracer = provider.get_tracer("example-agent")

def add(left, right):
    with tracer.start_as_current_span("execute_tool add") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "add")
        span.set_attribute("gen_ai.tool.call.id", "call-add-1")
        span.set_attribute("gen_ai.tool.call.arguments", json.dumps({"left": left, "right": right}))
        result = left + right
        span.set_attribute("gen_ai.tool.call.result", json.dumps(result))
        return result
```

Pass `trace_evidence=capture` to `create_run(...)` and call the instrumented
function between `run.get_input()` and `run.submit(output)`. Spans must start in
that step's capture window and end before submission preparation. Out-of-window,
unfinished, sampled or rejected observations are reported as losses, not moved
to another step. No instrumentation means no invented observations.

## Content, limits and privacy

- JSON strings from OTel are decoded at most once. Ordinary non-JSON text results
  stay text; valid JSON null is distinct from a missing field.
- Each argument/result allows at most **4 MiB canonical JSON** (sorted keys,
  ASCII escaping, compact separators, finite numbers) and depth 32, root zero.
  Cycles and unsupported objects are rejected. Metadata's default 256-character
  limit does not cut tool bodies. Trace capture remains 8 MiB per Run by default;
  the whole Runtime Evidence envelope is limited to 5 MiB and official multipart
  to 8 MiB including other parts/framing. Server limits may be smaller.
- Overlarge/invalid/sensitive bodies are omitted **whole**, with explicit field
  status and partial capture. Mandatory existing sensitive scanning cannot be
  bypassed by `allow_sensitive=True`. Upload-total overflow is an error, never a
  silent hash-only fallback or truncated JSON.
- Trace/span/parent IDs, timing, kind, original `unset|ok|error` status, safe
  attributes/events/resource/scope and up to 128 real links are retained within
  capture limits. The transport supports up to 10,000 spans, not a new default;
  the default capture cap stays 200.

## Completeness and interpretation

Each tool span has `tool_content_status.arguments` and `.result`: `present`,
`not_recorded`, `sensitive_content`, `size_limit` or `invalid`. Absent content is
not evidence that the tool did not run. `not_recorded` adds a reason/partial status,
not a fabricated dropped record. Actual omissions increase counters. OTel
`UNSET` is not failure, and complete capture does not prove all Agent actions
were instrumented. Logs and telemetry are observations, not authorization or
independent proof of external state.

The existing Trace artifact carries `trace_evidence`, `capture_status` and
`capture_summary` only with negotiated `runtime_trace`. SHA-256 and size bind the
same canonical Trace bytes. The Judge receives this as **untrusted telemetry**;
it must not treat it as verified tool execution. It is not copied into public
reports. Historical hash-only records do not gain reconstructed content.

## Upgrade and instrumentation prerequisites

After the public main contains this change, upgrade in the same environment
that runs your Agent, then restart that process:

```sh
python -m pip install --upgrade "kuma-defuzex[otel] @ git+https://github.com/DefuzeX-AI/KUMA-DefuzeX.git@main"
python -c "import kuma; print(kuma.__file__)"
```

For reproducibility replace main with the published commit. The version string
alone need not distinguish Git installs. Installing OTel does not instrument
tools: an ordinary span may have no arguments/results. Existing instrumentation
must record the actual semantic attributes in the active step.

There is no automatic typed `command_result`/`test_result` producer. Recorded
command/test observations can travel in tool Trace content, but KUMA does not
run tests, parse stdout, or invent components or execution success. Missing
observations never establish that an action did not execute.

## Optional file diffs

Use `create_run(..., track_files=True, upload_diff=True)` to send safe unified
patches as the separate `file_diff` capability alongside Trace. Default False
keeps file hashes only. Unsupported servers fail before Judge POST rather than
silently discarding the requested patch. Each patch is at most 32,768 UTF-8
bytes; combined patches at most 65,536 per envelope. No truncation or whole-file
upload. Binary, oversized, sensitive, unchanged or incomplete captures retain
hash metadata and an explicit omission reason. See [Runtime Evidence](runtime-evidence.md).

```sh
PYTHONPATH=src python tools/verify_runtime_trace.py
```

The verifier uses real local OTel and a fake external transport, with no service,
credentials or model.
