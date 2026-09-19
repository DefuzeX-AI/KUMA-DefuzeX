# Observe an Agent locally without evaluation

Optional [cloud storage](cloud-observations.md) requires a separate explicit
call. `observe()` and `export()` never upload automatically.

For pinned, executable OpenAI/LangChain/LangGraph examples with network disabled,
see [instrumented examples](instrumentation-examples.md).

`kuma.observe()` records supported spans from an **already instrumented** Agent.
It does not run your Agent, create a Case, call Judge, read credentials, contact
KUMA, upload data or spend service credit. This local API works without an account.
It is distinct from `create_run`, which implements a Case-to-Judgment evaluation.

## Runnable local example

This API targets the upcoming **0.3.0** release; the currently published 0.2.8
package does **not** provide `kuma.observe`. Until 0.3.0 is released, use a source
checkout containing this feature. Run these commands **from that checkout's
repository root**, where `pyproject.toml` and `examples/local_observation.py` exist:

```bash
python -m pip install -e ".[otel]"
python examples/local_observation.py
```

The example file is part of the source checkout, not an installed-wheel command.
It performs arithmetic and creates actual local OTel tool spans. It uses
no model or network. In an existing Agent, use its provider and instrumentation:

```python
import kuma

# Your Agent has already configured the global OTel TracerProvider.
with kuma.observe(external_run_id="job-42") as observed:
    result = my_agent.run("your task")

print(observed.render_text())
export = observed.export()  # detached, redacted plain JSON
observed.save("observation.json", root=".")  # explicit; new file only
```

Replace `my_agent.run` with your actual Agent call. `observe` does not install a
provider, auto-instrument frameworks, configure exporters or change sampling.
Without compatible instrumentation, the Agent still runs and the report says
`capture_status="failed"` with `trace_capture_failed`. Empty capture is **unknown**,
not proof that no tools were used. Supported OpenInference mappings are described
in [OpenInference capture](openinference.md); this is not a claim that all Agent
frameworks or every instrumentation version are supported.

## Arguments and results

| Argument | Default | Meaning |
| --- | --- | --- |
| `tracer_provider` | `None` | Existing provider with `add_span_processor`; `None` resolves the global provider at context entry. Unavailable/incompatible providers degrade capture without blocking the Agent. |
| `external_run_id` | `None` | Optional caller-owned correlation ID, 1-128 ASCII characters: initial letter/digit, then letters/digits/`.`/`_`/`:`/`-`. Not an authorization token; sensitive IDs are rejected. |
| `external_invocation_id` | `None` | Optional invocation ID with the same constraints, independent of the run label. |
| `limits` | `TraceEvidenceLimits()` | Existing bounded Trace policy. Observation requires at least 2048 total bytes; additionally caps the entire export at 5 MiB and 10000 retained spans. Default retained-span cap is 200. |

`observe` returns a one-shot `ObservationSession` supporting `with` and `async with`.
After exit:

- `export()` returns a detached JSON graph. Mutating it cannot alter the session.
- `render_text()` returns a concise timeline of span name, operation, duration and
  OTel status, plus completeness and gap reasons. It does not print body content.
- `save(path, root=...)` returns the published absolute `Path`. Both arguments are
  required. Relative `path` is relative to `root`, not process cwd. The root and
  destination parent must already exist; paths must stay within root. Links,
  reparse points, mount escapes and overwriting existing files are rejected.
  Publication is atomic, with owned descriptors and temporary files cleaned up.
  This call is explicit and separate from context exit; save failures therefore
  cannot replace an Agent exception. No automatic directories or files are made.

Invalid IDs raise `ValidationError(code="observation_invalid")`. Invalid limits,
re-entry, unfinished export or unsafe/failed save raise `ConfigurationError`.
Local errors do not expose raw OS messages or host paths through exception chains.

## Capture lifecycle and privacy

Capture includes spans **started and ended inside** the context. Spans started
before entry are not adopted; spans still open at exit are counted as
`trace_span_outside_window`. Nested contexts own only their own starts. Async
tasks inherit Python ContextVars; plain new threads must explicitly propagate
context if they should belong to an observation. Concurrent observations are
isolated. Cross-thread endings of registered spans retain their original owner.

Exit never calls the provider's flush or shutdown: user exporters may perform
network I/O and remain wholly user-owned. One inert reusable processor remains
attached per provider because OTel has no public detach operation; session
registrations are removed at exit. KUMA installs no background worker or exporter.

Canonical Trace allowlists, sensitive-data redaction, JSON validation and body
bounds apply. Safe recorded model/tool bodies may be retained, not arbitrary raw
logs, credentials, repository content, hidden reasoning or invented tool calls.
Redaction is best-effort recognition, not a guarantee against every unknown secret
format. Review the export before sharing it. Optional model fields are omitted
whole under budget pressure before removing spans; no body is partially truncated.
Sampling, filtered attributes, unavailable bodies and incomplete topology remain
visible in `reasons`, `capture_summary`, `dropped_count` and `truncated`.

Agent exceptions propagate unchanged. The export retains only a categorical
execution status, not exception messages, reprs or tracebacks. A normal context
exit means `execution_status="completed"`, **not** a passing evaluation. An ordinary
exception means `failed`; interruption/cancellation means `aborted`.
`duration_ms` is client monotonic context elapsed time (including user waits), not
model compute time or server queue latency. Values beyond 24 hours are `null`.
`evaluation_status` is always `not_performed`; no cost or usage zeros are fabricated.

## Local export schema

The closed `kuma.observation.v1` object contains:

| Field | Type / meaning |
| --- | --- |
| `schema_version` | Literal `kuma.observation.v1`. |
| `observation_id` | Locally generated `obs_` plus 32 lowercase hexadecimal characters; no Case or account identity. |
| `external_run_id`, `external_invocation_id` | Validated caller labels or `null`. |
| `execution_status` | `completed`, `failed`, `aborted`, or `unknown`; not a Judge verdict. |
| `evaluation_status` | Literal `not_performed`. |
| `duration_ms` | Integer 0..86400000 or `null`, context elapsed time. |
| `sdk_version` | Version of the collecting SDK. |
| `spans` | Existing normalized Trace spans, ordered by start time and identity; supported optional `model_content` uses `kuma.model_content.v1`. |
| `capture_status` | `complete`, `partial`, or `failed`, consistent with losses; empty capture is failed/unavailable. |
| `capture_summary` | Existing `defuzex.trace_capture_summary.v1` counts and topology facts. This API observes spans, not native log records; log counters are zero. |
| `reasons` | Sorted existing stable Trace gap/reduction reasons. |
| `dropped_count` | Nonnegative bounded aggregate loss count. |
| `truncated` | Whether span-side truncation/loss was observed; body field statuses independently describe omissions/redaction. |

Canonical export uses sorted keys, compact JSON, ASCII escaping and finite values.
Cloud history is a separate explicit feature; this local API never uploads on exit.

## Offline example

From a source checkout with the optional OTel dependencies installed:

```bash
python examples/local_observation.py
```

The example uses real local spans and temporary files, not Backend or model services.
