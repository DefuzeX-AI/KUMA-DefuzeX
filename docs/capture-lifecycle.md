# Capture normal Agent execution, export, then Judge

New in KUMA 0.5.0. Opt in once around your normal Agent call. KUMA wires the selected
supported instrumentation, records actual ended calls and automatically saves a
redacted local capture. A later installed-SDK command exports or explicitly reviews
that capture. You do not construct ATIF, convert events or create a requirements file.

## Normal use

Install the extra matching your Agent, for example:

```sh
python -m pip install --upgrade "kuma-defuzex[capture-langgraph]==0.5.0"
```

Keep your Agent's normal model/tool configuration. Wrap its ordinary invocation:

```python
from kuma import capture

task = "Find the opening hours and cite the page you used."
with capture(task, framework="langgraph") as recorded:
    answer = my_agent.invoke({"messages": [("user", task)]})

print(recorded.capture_id)
print(recorded.persistence_status)
```

`my_agent` is your existing configured Agent. KUMA does not replace its model,
tools, runtime or input. No Kuma credential is needed for this local phase. Set
the original task at the start, before observing the outcome. Unknown tools remain
unknown; KUMA does not invent tool rules, reference answers or task success.

After exit, use the printed `cap_...` ID in a separate process:

```sh
python -m kuma captures list
python -m kuma captures export cap_REPLACE_WITH_PRINTED_ID --output capture.json
```

The destination must be a new file in an existing directory. Local export is atomic
and never overwrites a file. It includes the original task/rules and observation,
including partial/failed status, omissions and exact recorded source IDs.

For explicit hosted review, set your ordinary `KUMA_API_KEY`, then:

```sh
python -m kuma captures judge cap_REPLACE_WITH_PRINTED_ID --output review.json
```

The key needs `sdk:observe` and `judge:run`. No DeepSeek, OpenRouter or storage
credential is required. Agent runtime model/tool credentials are independent.
Ordinary capture/list/export never upload or invoke Judge. Explicit `judge` uploads
the saved observation and bound task, then invokes the existing fixed-fee service.
Only a completed validated review settles the configured Judge fee. It never
regenerates Cases or reruns your Agent.

If the outcome is unknown after timeout, run the same capture/arguments again. The
default logical key hashes the immutable capture ID and exact prepared submission.
Changing only the output filename does not request a new review. Changing rules or
explicitly selecting a new `--idempotency-key` does. Existing service idempotency
protects the same logical request; KUMA does not retry model calls automatically.

## Actual support and boundaries

| Selection | Optional installation | Verified ordinary execution | Scope and gaps |
| --- | --- | --- | --- |
| `openai` | `[capture-openai]` | Official OpenAI Chat Completions client, real request/response parsing, OpenInference model span, input/output and usage | Synchronous Chat Completions only in this matrix. This is not OpenAI Agents SDK coverage; arbitrary Python tools outside instrumentation are unknown. Responses/streaming/async clients are not claimed. |
| `langchain` | `[capture-langchain]` | Actual Runnable, `@tool` invocation, chat-model callback; model/tool arguments, result, status and parents | Supported ended OpenInference/OTel spans. Uninstrumented code is not reconstructed. |
| `langgraph` | `[capture-langgraph]` | Compiled StateGraph, actual nodes/tool/model callbacks and parent relationships | Same LangChain OpenInference hook. Captures context-propagated calls; work detached into unpropagated contexts can be missing. |
| `existing` (default) | Existing application-owned OTel SDK/instrumentation, or `[otel]` when setting up your own provider | Reuses compatible provider without installing framework hooks | An uninstrumented Agent produces zero capture, visibly failed; it is never represented as a complete tool/model trace. |

The exact executed optional matrix is in
[requirements-instrumentation.txt](../examples/requirements-instrumentation.txt).
The 0.5.0 owner checks use Python 3.12 on Windows and the same pinned OpenAI3.16.2,
LangChain Core1.6.3, LangGraph1.2.11, OpenInference0.1.60/0.1.76 and OTel1.44.0
versions. Extras declare bounded dependency ranges; this matrix does not certify
every combination in those ranges. Follow the pinned matrix for reproduction.
Instrumentation configuration and privacy hiding remain governed by the
[OpenInference configuration contract](https://github.com/Arize-ai/openinference/blob/main/spec/configuration.md).

Explicit selected modes install a process-wide hook once, without setting a global
provider, installing an exporter or replacing sampling. A compatible existing
global provider is reused. Otherwise KUMA creates a private no-exporter provider.
Hooks remain installed for reuse, and KUMA never uninstalls a user hook or shuts
down their provider. Rebinding a KUMA-owned hook to a different provider is refused.
App-owned hooks remain untouched; `external_binding_unverified` records that KUMA
cannot independently infer their provider binding. Supply the actual provider or
use `existing` mode with it. Zero spans are still visibly failed.
Duplicate-hook detection covers the selected OpenInference instrumentor, not every
third-party vendor. If a different vendor already instruments the framework, choose
`existing` with that provider instead of opting into a second instrumentation hook.

The capture context supports `with` and `async with`. Async context/cancellation
is tested independently of the synchronous integration matrix above; that alone
does not certify every asynchronous model API. No arbitrary/closed-source Agent
visibility is promised. No synthetic root is counted as a model/tool call.
Start the window before the traced Agent root. The existing observation contract
requires retained parents: if capture starts inside an already active, unrecorded
parent span, descendants can be omitted with `trace_topology_partial`, potentially
leaving zero spans/failed capture. KUMA does not fabricate a replacement root or
rewrite parent IDs. Such empty capture is exportable but rejected before Judge.

## Python API

`capture(task, *, framework="existing", tracer_provider=None, repo_path=".",
storage_path=None, tools=None, requirements=None, limits=None)` returns a one-shot
`CaptureSession`; construction validates local options and entry performs setup.

| Argument | Meaning |
| --- | --- |
| `task` | Required original task text, nonblank1..12000 characters. Recognized secrets are redacted before saving, with an explicit limitation; caller input is not mutated. |
| `framework` | `existing`, `openai`, `langchain` or `langgraph`. Unsupported/missing selected integration fails clearly before the Agent block; no runtime dependency installation. |
| `tracer_provider` | Optional application SDK-compatible OTel provider; preserved. None resolves the global/established selected hook as described above. |
| `repo_path` | Existing Agent workspace, default current directory. Used only to enforce external storage selection; no recursive Agent workspace scan. |
| `storage_path` | Existing external artifact parent, relative to repo_path or absolute, disjoint from workspace. None defaults to `~/.kuma-captures`; only this opt-in API creates its default root and `.kuma/captures`. No workspace fallback. |
| `tools` | Optional complete v1 tool policies from the [task/tool contract](trajectory-platform.md). None means unknown; [] explicitly declares no tools. Observed tool names do not become invented policies. |
| `requirements` | Optional advanced complete v1 profile retaining the exact original task as objective; mutually exclusive with a supplied tools list. No file is required for basic use. Sensitive advanced policies require correction, rather than silently rewriting rules. |
| `limits` | Optional `TraceEvidenceLimits`; defaults reuse observe limits, minimum2048 bytes, hard5MiB/10000-span observation ceilings. Actual field/body/topology omissions remain visible. |

After exit, `session.capture_id` identifies the durable file. `persistence_status`
is `not_saved`, `saved` or `failed`; `persistence_error` is null or the safe
`capture_save_failed` category. `session.export()` returns detached finalized
capture JSON without I/O, including recovery data after a save failure when
finalization succeeded. Before finalization it raises `ConfigurationError`.
`CaptureSession` should be constructed through `capture()`; it is one-shot.

Normal execution completion means the Agent returned, not that its task succeeded.
Entry checks current storage write permission before the Agent block; later disk
failures remain possible.
Agent errors remain the original errors; cancellation remains cancellation. KUMA
saves failed/aborted execution with whatever evidence was actually retained. If
the disk also fails, it does not replace the original Agent exception; inspect
persistence_status/error and the in-memory export. On normal completion, save
failure raises a safe `ConfigurationError` rather than pretending persistence worked.

`list_captures(*, repo_path=".", storage_path=None, limit=20)` returns metadata-only
summaries, newest local file first. limit is integer1..100; no automatic pagination,
task/body output, authentication or network call. Each row contains capture_id,
execution_status, capture_status, span_count, operation_counts and reasons. Counts
refer only to retained actual model/tool/other spans, not inferred activity.

`export_capture(identifier, path, *, repo_path=".", storage_path=None)` atomically
exports the durable task-bound document to a new caller-selected file and returns
its absolute Path. The parent must exist; existing/link/unsafe paths are rejected.

`judge_capture(identifier, *, repo_path=".", storage_path=None, client=None,
requirements=None, idempotency_key=None)` explicitly returns a completed/stopped
public review and retains the validated response locally. identifier is a local
capture ID; client is an optional KumaClient, default ordinary Kuma credentials.
requirements optionally adds known rules while retaining the original task.
idempotency_key is optional8..200 ASCII letters/digits/`._:-`, first character
alphanumeric; None derives the stable key. Empty evidence, malformed key, invalid
rules and currently unwritable result storage are rejected before upload/Judge.
The returned rules hash must match the submitted task/rules. Auth/scopes, credit,
timeout and service failures retain existing `KumaError` behavior, no hidden retries.

## Installed CLI contract

All commands accept `--repo-path` (default `.`) and `--storage-path` (default external
user storage). Use the original path arguments when selecting a custom store or
working from another directory. There is no daemon, remote collector or database.

| Command | Arguments and behavior |
| --- | --- |
| `captures list` | Optional `--limit` integer1..100, default20. Prints only local summaries. An absent/unreadable store fails clearly without creating one. |
| `captures export ID` | Required local ID and `--output` new path. Writes complete `kuma.capture.v1` JSON atomically; no overwrite, network or fee. |
| `captures judge ID` | Required local ID and `--output` new path. Optional `--idempotency-key`, `--requirements` advanced UTF-8 JSON file ≤256KiB, `--base-url` public Kuma Backend URL (or KUMA_BASE_URL/default hosted URL), and `--timeout` positive finite per-request seconds, default180. Never a model-provider URL. |

Judge CLI preflights its output and SDK result-store write permission before paid
work. A future disk fault/collision can still occur. The validated review is kept
in external capture storage before publishing the requested output; an output
collision never replaces another file. Recover with the same capture/rules/key and
a new output path. If even local retention fails, the completed server review is
recoverable using that same logical request. No later output failure refunds an
already completed review or silently requests another one.

CLI exit0 means successful local operation or completed review; it is not a task
success verdict. Judge exit2 saves an explicit stopped report/coverage; exit1 is a
safe local/API failure. Invalid command syntax uses normal argparse exit2. Output
filenames, rule files and credentials are never automatically discovered.

## Durable data, privacy and evidence gaps

`kuma.capture.v1` has exactly schema_version, capture_id, requirements, observation
and instrumentation. capture_id is `cap_` plus32 lowercase hex characters. The
requirements field uses the existing v1 task/tool contract. observation is the
closed, redacted [observation contract](observation.md), retaining original span/
trace/parent IDs, timestamps, recorded arguments/results/status and loss accounting.
instrumentation contains only framework and status (`application_owned`,
`kuma_enabled`, `kuma_reused` or `external_binding_unverified`).

Captures are immutable no-replace UTF-8 JSON files under the external parent's
`.kuma/captures`, at most6MiB each, with bounded no-follow reads. Review snapshots
are content-addressed there, at most8MiB, and identical bytes are deduplicated.
There is no automatic historical deletion, indexing service or new billing tier.
Advanced task/tool rules remain the [existing requirements schema](trajectory-platform.md).
The hosted prepared submission still has its existing4MiB limit: a larger saved
capture is exportable, but Judge preparation refuses oversize input rather than
silently shortening it. Retained history is owned by the local user.

External directory separation is not a process sandbox; an Agent with the same OS
permissions may still access it. Recognized-secret redaction and existing whole-
field omission apply before saving/submission, not a guarantee of detecting every
unknown secret format. User-owned exporters preserve their independent egress
behavior. No capture content or raw native errors are printed in CLI summaries.
Context loss, hidden/unrecorded bodies, filtered fields and sampling are not proof
that an Agent avoided tools or succeeded. Review the recorded scope and gaps.

## Capture privacy and operation provenance

Capture/trajectory review additionally redacts recognized email addresses, IPv4
spellings and user-home paths before local persistence and review preparation.
This matches the review service's known-PII boundary, not a promise to discover
all personal data. Original Agent inputs and user exporters are not modified;
ordinary source references and source fingerprints remain unchanged. Retained
model/tool bodies expose `redacted` status and capture reasons, rather than
pretending their original content was fully preserved. This does not change the
privacy policy of ordinary Case/Run Evidence.

For the supported KUMA-managed LangChain integration, callback `run_type` from
the exact same run/span supplies operation identity when native
`gen_ai.operation.name` is absent. `llm`, `tool`, and `chain` map to `chat`,
`execute_tool`, and `invoke_workflow`. Span names and model tool proposals never
establish model/tool execution. Existing native identity, including unknown
values, wins; external instrumentation is not patched or rebound.

Optional span `operation_source` has exactly `source="langchain_callback"`,
`run_type` (`llm|tool|chain|retriever|unknown`) and `openinference_kind`
(`LLM|TOOL|CHAIN|AGENT|RETRIEVER|unknown`). It preserves the original vendor kind;
the ended vendor span and user exporters remain unchanged. Conflicts and unknown
types are visible as `trace_operation_type_conflict` and
`trace_operation_type_unknown`. Unknown/retriever callbacks cannot promote a
span to model/tool. Missing model bodies retain `trace_model_content_not_recorded`.
These are client observation claims, not server-certified execution. Historical
objects without this optional field remain valid.

If KUMA installs a hook but cannot attach its source callback, capture setup fails
with a configuration error. Retrying repairs or rejects that same owned hook;
it is not relabeled as externally configured success. If an owned callback later
cannot resolve its span identity, the capture window reports an unknown-type gap
without inventing per-span provenance. Original callback results and exceptions
still propagate, and callback-local context is always reset.

## Reproduce without a paid model

From a source checkout install the pinned optional matrix, then:

```sh
python examples/capture_lifecycle.py --framework langgraph --repo-path /existing/agent --storage-path /existing/external
```

The example executes a real graph/tool/chat callback; only the external model is
replaced. It prints the durable ID and actual capture status, without uploading or
Judging. Use the installed CLI above for separate-process export. The source-side
`tools/verify_capture.py` verifies real frameworks plus separate-process CLI/Judge
HTTP recovery against a recorded synthetic protocol response. Set
`KUMA_RUN_INSTRUMENTATION_TESTS=1` for the optional real matrix; skipped optional
tests are not a successful integration run. The HTTP fixture is a protocol test,
not an actual quality assessment of the sample arithmetic task or live billing.
