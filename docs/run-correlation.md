# Run correlation and stage timings

This API is available in KUMA 0.3.0 or later. Official correlation also requires
server support. To run the repository example, use a source checkout:
`python -m pip install -e .`, then
`python examples/run_correlation.py`. It uses local custom Providers only.

## Use caller execution IDs

Pass `external_run_id` to `create_run()` and/or `external_invocation_id` to
`run.submit()`. Each defaults to `None`. Labels are ASCII, 1-128 characters,
start with a letter/digit and thereafter allow letters, digits, `.`, `_`, `:`,
`-`. Use opaque non-secret labels, not paths, keys, prompts or user content.
Invalid values raise `ValidationError(code="run_context_invalid")` locally.
Labels correlate executions; they do not authenticate users or prove execution.

The single official Judge request negotiates `kuma.run_context.v1` through
`supported_run_context_schemas`. Omitted labels leave existing wire metadata
unchanged. Explicit labels require support and captured Runtime Evidence for
every step; otherwise `ConfigurationError(code="run_context_unsupported")`
occurs before POST. Batch Judge does not support this metadata and rejects it
before configuration requests. Custom Judges receive the frozen context locally.

The closed context has `schema_version`, `run_id`, `case_id`, and 1-50 `steps`.
Each step binds its real `input_id`, `step_id`, `submission_id`, the two nullable
external labels, `execution_status`, and nullable `client_step_elapsed_ms`.
Duration is an integer from 0 to 86,400,000 milliseconds. Submission IDs are
unique. No body, credentials or private Judge information is added here.

## Inspect what happened

`run.timeline` returns a detached JSON-compatible `kuma.run_timeline.v1` dict:

- `run_id`, `case_id`, `run_state`: local lifecycle identities and state.
- `execution_steps`: committed execution statuses, labels and input-to-submit
  intervals. An Agent failure is not a Judge failure.
- `capture_steps`: capture status, missing components and dropped counts for
  each committed Input. Partial capture is not a failed Agent execution.
- `evaluation_status`: `not_performed`, `running`, `failed`, or `completed`.
  `completed` means a valid Judge response, not necessarily a passing verdict.
- `run_receipt`: validated server receipt after successful evaluation, else null.
- `stages`, `dropped_stages`: bounded measured intervals and overflow count.

Each stage contains `stage`, nullable `elapsed_ms`, `status`, `source`, and
nullable `input_id`. Each local collector retains at most 200 records; Run may
combine its collector and the Official Provider collector. Reading this property
makes no network request and mutating the returned value cannot alter the Run.

| Stage | What the interval actually measures |
| --- | --- |
| `case_generation_request_wait` | Case Provider call, including request/wait for official generation; local/custom Providers may perform no network I/O. |
| `agent_input_to_submit` | First Input delivery until submission; includes caller idle time, not measured Agent CPU/model time. |
| `evidence_prepare` | Local Evidence preparation. |
| `evidence_upload_request` | Multipart request through initial response, including server acknowledgment, not pure upload bandwidth. |
| `judge_request_wait` | Official Provider call including preparation, upload and polling. |
| `judge_provider_call` | Whole local Judge Provider invocation and report validation. |
| `server_queue`, `model_compute` | Unknown: null duration with source `unavailable`. No server timing contract exists here. |

Client measurements use a single monotonic clock. Intervals overlap and must not
be added together. No cross-host timestamp subtraction is performed. Interrupted
or restarted processes do not recover missing timings as zeros. Retries retain
separate attempts rather than overwriting failure evidence.

## Receipts, replay and privacy

An official result carries a closed `kuma.run_receipt.v1`, retained under
`report.extensions["run_receipt"]`. It binds receipt/Run/Case/Judgment/public
operation identities, echoes steps, and reports accepted Evidence and bounded
Trace capture metadata. Malformed, missing expected or mismatched receipts fail
with `ProviderError(code="invalid_response")` before pending-operation clear;
retry resumes GET-only once an operation ID is known.

`judgment_reused` explicitly distinguishes cached evaluation. In that case
`source_judgment_id` and `source_evidence_ids` refer to the original Judgment and
its Evidence, while capture metadata describes the current request. The SDK
does not claim the new Evidence was reevaluated. Context affects request identity
but not the semantic Judge cache or model prompt.

Fresh-process request recovery validates the receipt against persisted
Run/Case/operation identities. Original external labels and local timing are not
stored in the identity-only request ledger and cannot be independently compared
after restart. The received receipt remains server-provided, not local telemetry.
