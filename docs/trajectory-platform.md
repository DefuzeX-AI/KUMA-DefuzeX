# Agent execution review workspace

## Delete an owned trace

Use the existing workspace request method with the exact ID returned by upload:

```python
workspace = client.trajectory()
result = workspace.request("traces.delete", {"id": trace["id"]})
print(result["deleted"])  # True when removed; False if already absent for this owner.
```

This write requires `sdk:observe` for an API key (or the existing workspace write
permission for JWT). Only the selected owned trace is deleted and its storage
quota reclaimed. Reports, dataset snapshots and experiment snapshots are not
cascade-deleted; already-started Judge work is not cancelled. Later trace reads
and new Judge requests return not-found. Foreign-owner or wrong-kind IDs return
404 from the service. The SDK rejects malformed/wrong-kind input locally.
Deleting raw traces does not require `redact_sensitive=False`: no source content
is returned. The closed result is exactly `{id, deleted}`, with the same requested
ID and a Boolean. No automatic retry or new convenience API is introduced.

If workspace storage is full, Judge may return HTTP 507 as `ServiceError` with
code `workspace_storage_exceeded`, `retryable=False`, and the message
"Workspace storage is full. Contact support before running Judge again."
The SDK preserves the response request ID and does not automatically retry this
error. It is distinct from temporary processing `capacity_exceeded`; increasing
retry counts does not free storage. No storage quota is increased by this patch.

## Explicit privacy policy (0.7.0)

Capture, `prepare_review`, `prepare_atif`, `prepare_observation`,
`prepare_submission`, requirements validation and `TrajectoryClient` operations accept
`redact_sensitive: bool = True`. Default behavior remains protected. Explicit `False`
retains bounded original domain content after a content-free `RawContentWarning`;
it does not recover earlier redaction, disable authentication or enlarge size/shape
limits. Preparation stays local. Upload/Judge may store originals in private Core
and send them to the configured model provider. See the [capture lifecycle privacy
guide](capture-lifecycle.md#explicit-original-content) for timing and persistence.

```python
workspace = client.trajectory()
uploaded = workspace.upload(submission, redact_sensitive=False)
review = workspace.evaluate(
    uploaded["id"], idempotency_key="owned-review-001", redact_sensitive=False
)
stored = workspace.request("reviews.get", {"id": review["id"]},
                           accept_media_omission=True, redact_sensitive=False)
original_json = workspace.export("reviews", review["id"],
                                 accept_media_omission=True, redact_sensitive=False)
```

Only explicit `False` is sent at command top level and included in request identity;
omit/`True` retains old wire/hash semantics. Same direct key plus changed policy
conflicts, never silently re-evaluates. `run_experiment` includes raw policy in its
automatic child keys; changing it can start new paid reviews. Later reads and
transitive reuse need an explicit opt-out again. Protected readers safely refuse raw
artifacts; raw reads return owned original bytes, not a re-redacted representation.
Stored payload and safe summary wrappers have consistent `content_privacy: "raw"`;
the original submission/source/hash is not rewritten. Dataset, experiment, feedback
and review lineage retains this marker. Summaries/logs/errors do not expose secrets.

Classic Case/Run, cloud Observation and legacy `allow_sensitive` are unchanged.
Media scope is an independent capability and still requires its own read flag.
For standard OpenAI clients using the existing model gateway, pass
`extra_body={"redact_sensitive": False}` only when intentionally sending originals.
The gateway consumes this field before forwarding; it is not a model-provider
parameter. Direct gateway responses expose a generic `X-Kuma-Content-Warning` header.
KUMA adds no model client and does not change your Agent's input or external exporters.

Available in KUMA 0.4.0, this capability is part of KUMA, not a second SDK. It connects local capture,
task/tool requirements, stored traces, datasets, process Judge reports, human correction and
version comparisons through the existing public Backend. The hosted trajectory Judge is enabled.
Self-hosted use requires compatible enabled services; clients never configure private storage.

### Text-and-structure review with unreviewed media

KUMA 0.6.0 high-level `evaluate`, `judge_capture` and `run_experiment` default to
`accept_media_omission=True`. Compatible services omit recognized unsupported
media only from model input, retain structured task/tool facts, and review the
remaining text. Original capture, stored trace, source hashes and exact trace
exports remain unchanged. No image/audio/video understanding is performed.

Inspect `coverage.media_omission` when present: `scope="text_and_structure"`,
`media_understanding="not_performed"`, bounded omission coordinates/hashes and
`reviewable_delivery_complete` describe this restricted review. Physical
`source_delivery_complete` remains false. `completed` is neither full-source
review nor proof of Agent success/failure. Safe completed restricted review uses
the existing fixed Judge fee; unsafe or pure-media inputs stop before model work
and are not charged. External references are not fetched; detection is bounded,
not a guarantee of recognizing every encoding.

Pass `accept_media_omission=False` (CLI `kuma captures judge ... --reject-media`)
to retain legacy rejection and automatic capture/experiment keys. True adds mode
to automatically generated keys. Changing a directly submitted manual review key's
mode conflicts; keep the original mode on recovery. Experiment parent keys are
local seeds, not remote operations: switching mode generates new child keys and
can request another paid review. No automatic retry or re-review occurs.

Low-level `workspace.request(...)` remains legacy by default. For scoped review
reads, lists, feedback and experiment operations, explicitly supply
`accept_media_omission=True`; `workspace.export(...)` does so by default. Old
readers receive a safe compatibility error for scoped artifacts rather than a
rewritten report or silently filtered page. Original trace reads stay unchanged.
Scoped/full-source experiment cases are not blindly compared. Upgrade compatible
services before using the new option; the SDK never silently falls back.

```python
review = workspace.evaluate(trace_id, idempotency_key="review-text-scope-01")
omission = review.get("coverage", {}).get("media_omission")
if omission:
    print("Text and structure only; media was not reviewed.")
saved = workspace.request("reviews.get", {"id": review["id"]},
                          accept_media_omission=True)
```

No separate storage or model key belongs in your Agent. `KumaClient().trajectory()` reuses your Kuma
credential. Local capture and preparation never upload automatically. Your ordinary Agent
model/tool credentials remain your application's responsibility.

## Quick start: one Kuma Key

Install `python -m pip install --upgrade kuma-defuzex`. Set `KUMA_API_KEY` to an ordinary Kuma
key with `sdk:observe` and `judge:run`; reads/exports additionally need `sdk:read`. Existing keys
are not silently granted scopes: check `kuma whoami` and use a key with the required permissions.
No DeepSeek, OpenRouter or database key is needed. Agent execution/model/tool credentials remain
your Agent's responsibility; reviewing an existing trace does not run the Agent again.

```python
import json
from pathlib import Path
from kuma import KumaClient

workspace = KumaClient().trajectory()
document = json.loads(Path("trace.json").read_text(encoding="utf-8"))
requirements = json.loads(Path("requirements.json").read_text(encoding="utf-8"))
capture = workspace.upload_atif(document, requirements)
review = workspace.evaluate(capture["id"], idempotency_key="my-agent-run-001-review-01")
print(review["review_status"])
```

`trace.json` is an existing ATIF-v1.0 through ATIF-v1.8 export. `requirements.json` describes the original task and
known tool rules using the template below. Unknown information stays null/empty rather than being
invented. Local preparation and upload do not run Judge. `evaluate` is an explicit fixed-fee
operation; only validated completed reviews settle the account's configured Judge fee. Inspect
`review["report"]` and `review["coverage"]`; `stopped` is an incomplete review, not an Agent failure.
Reuse the **same key and input** after a timeout; a fresh key requests a separate paid review.

The runnable `examples/review_trajectory.py` creates a blank template with
`--write-requirements-template requirements.json` (no key/network), then accepts
`--trace trace.json --requirements requirements.json --output review.json`. It supports ATIF,
prepared KUMA bundles and ObservationSession exports, writes exclusively and derives a stable
review key from the immutable capture ID. It validates an explicit request key and reserves a
writable exclusive output file before any network call. On failure it removes only its own
empty reservation, so the same input/key/output can be retried. Nonempty partial output remains;
choose a new output path to recover. Later disk failures cannot be prevented by preflight.
Repeating identical input recovers the same review; use `--idempotency-key` only when deliberately
selecting a different logical review.

## Describe the task and available tools

Start with `kuma.trajectory_requirements.requirements_template()`. Fill the actual rules that
applied during execution, not rules invented after seeing a result. Validate with
`validate_requirements(requirements)`. Both functions are local and have no paid side effects.
`requirements_fingerprint(requirements)` returns the SHA-256 of the validated canonical UTF-8
JSON. Changing content creates a different revision even if the declared version stays the same.

All fields of `kuma.trajectory_requirements.v1` are required. Use the documented empty/null
sentinels where information is unknown; never guess a tool's availability.

| Field | Meaning and bounds |
| --- | --- |
| `schema_version` | Exactly `kuma.trajectory_requirements.v1`. |
| `name`, `version` | Nonblank caller labels, at most 200/80 characters. |
| `task_type` | Caller classification, at most 120 characters; empty means unclassified. |
| `objective` | Original task, nonblank, at most 12,000 characters. |
| `success_criteria` | 1–16 conditions; each has a distinct `id` (1–64), `description` (1–1500), `priority` (`required`/`preferred`), and `evidence_expected` (0–8 nonblank strings, each ≤1500). These are requirements, not scoring gold labels. |
| `constraints`, `limitations` | Lists of up to 32 nonblank strings, each ≤1500; empty means no supplied constraints/gaps, not proof of completeness. |
| `tools` | `null`: unknown catalog; `[]`: explicitly no tools; otherwise ≤256 distinct named tool policies. |
| `retry_policy` | `normal_conditions` and `stop_conditions`, each a list bounded like constraints. Unknown rules may remain empty; retries receive no blanket penalty. |
| `budgets` | `duration_ms`/`tool_calls`: integers; `cost_usd`: finite number. Each is null (unknown) or nonnegative ≤10¹². Zero is a real zero budget. |
| `reference` | Optional expected outcome/reference, null or nonblank ≤12,000 characters. A reference is not proof of execution. |

Each tool policy contains all of: `name` (1–200), `description` and `availability` (1–1500),
`input_schema`/`output_schema` (JSON objects or null), and `use_when`, `avoid_when`,
`prerequisites`, `effects`, `expected_errors` (0–16 nonblank strings of ≤1500 characters).
Schemas describe tools; Judge never executes them. Tool policies state when use makes sense,
when it does not, required permissions/context, effects and expected failures.

Profiles are limited to 256 KiB, finite JSON and depth 32. Core additionally limits the formatted task policy (excluding the tool catalog) to 60,000 characters; larger policies are rejected before review instead of silently shortened. Unsafe requirements are rejected for
correction rather than silently changing your rules. Automated privacy detection covers known
patterns; inspect your data and remove credentials before any explicit submission.

## Capture, prepare and explicitly submit

```python
from kuma import KumaClient
from kuma.trajectory import prepare_review
from kuma.trajectory_capture import prepare_submission
from kuma.trajectory_requirements import requirements_template

requirements = requirements_template()
requirements.update(name="Inspect dimensions", version="1", task_type="inspection",
                    objective="Inspect the object dimensions.")
requirements["success_criteria"] = [{
    "id": "width", "description": "Report the recorded width.", "priority": "required",
    "evidence_expected": ["Inspection result"],
}]
bundle = prepare_review(requirements["objective"], [
    {"event": "tool_result", "tool": "inspect", "ok": True, "result": {"width": 8}},
], tools=None)
submission = prepare_submission(bundle, requirements)

# Explicit network operations; KUMA_API_KEY supplies the same existing credential.
workspace = KumaClient().trajectory(timeout=180)
policy = workspace.save_requirements(requirements)
trace = workspace.upload(submission, agent_version="build-42", case_id="dimensions", tags=["smoke"])
report = workspace.evaluate(trace["id"], idempotency_key="dimensions-build42-review01")
```

For an existing `ObservationSession.export()` use `prepare_observation(observation, requirements,
sample="trajectory")`. It preserves recorded spans, IDs, timestamps, statuses, links and capture gaps.
`classify_observation(observation)` groups retained spans by explicit model/tool/retrieval/
embedding/Agent operation metadata; it never equates a tool error with Agent fault. These local
helpers return detached JSON and do not modify original captures or charge credits.

`prepare_submission(bundle, requirements, observation_id=None, inventory=None)` binds the
exact task/tool declarations and canonical requirements hash. `observation_id` is null or
`obs_` plus 32 lowercase hex digits. `inventory` is null or a structural capture inventory from
the helper. The exact submission fields are `schema_version`, `bundle`, `requirements`,
`requirements_sha256`, `observation_id`, `inventory`; full submissions are limited to 4 MiB.
Native bundles contain 1–10,000 ordered events; missing turns stay unknown.

### Existing App ATIF exports

`workspace.upload_atif(document, requirements, agent_version=None, case_id=None, tags=None)`
prepares and explicitly uploads an in-memory `ATIF-v1.0` through `ATIF-v1.8` JSON object. `requirements` is the
complete validated profile described above; optional labels have the same bounds as `upload`.
It returns the normal trace receipt, requires `sdk:observe`, and does not call Judge or execute
an Agent. Invalid input raises `ValidationError` before transport; network errors use the normal
Kuma error categories. Caller objects remain unchanged. Only the existing Kuma key is needed.

For local-only preparation, use `kuma.trajectory_atif.prepare_atif(task, document,
sample="trajectory", tools=None, limitations=())`, then `prepare_submission`. `task` is the
actual scope (1–12,000 characters); `sample` is a nonblank label of at most 200 characters.
`tools` has the catalog semantics above. `limitations` is a list/tuple of at most 32 known gaps;
format notes use remaining slots. The helper returns a detached v1 bundle, performs no I/O,
redacts recognized sensitive patterns and raises `ValidationError` for invalid/oversized input.

Full trajectory metadata and original steps are retained as portable records, including nested
subagents, unknown fields and exact JSON integers. Root `trajectory_id` may be absent or null.
Versions 1.0..1.6 require an existing string `session_id`, including empty/whitespace strings;
versions 1.7/1.8 additionally permit absent/null sessions. Those states are preserved, never
filled from a parent or converted into a document identity. Embedded
children must have nonempty IDs unique within the same parent's array; different parents may
reuse an ID. Sessions may repeat across siblings and are not reference-matching keys. Each
node retains its actual 1.0..1.8 version, including mixed-version documents; each node obeys
its own version's session requirement. Positive, unique
integer step IDs retain source order; historical nonsequential IDs remain supported. Four-MiB/depth-32 preparation and
the 10,000-record limit apply; transport overhead also counts toward submission limits. Hashes
refer to sanitized original content. External artifact references are retained but never opened.

When the root ID is absent/null or recorded IDs repeat across parent scopes, every envelope
uses a deterministic internal coordinate and `trajectory_id_origin="source_pointer_sha256"`.
The coordinate is `atif-derived:` plus SHA256 of canonical UTF-8 JSON
`["kuma.atif.coordinate.v1", sanitized_source_sha256, original_node_pointer]`
(unescaped Unicode, sorted keys, compact separators, finite numbers only). It is a source
location, not a recorded execution/session ID. Original `record` IDs and reference values
remain unchanged. Parent/turn coordinates use the same mode. Explicit globally unique IDs
keep the old envelopes without this origin field. Core independently verifies coordinates
against reconstructed sanitized source. Only these nine known versions' common record shapes
are supported, not arbitrary future versions or external media resolution. Legacy session-only
references remain exactly as recorded: KUMA does not apply newer reference-resolution rules,
rewrite them to modern IDs, infer causality, or fetch their targets. This is lossless partitioning,
not a full Harbor schema validator or format migration. Compatible server support is required for
new formats/derived coordinates; older servers may reject them rather than silently rewrite.

Report locations add `source_pointer` (original JSON pointer), `trajectory_id`, nullable
`parent_trajectory_id`, nullable `atif_step_id` and nullable `turn_origin`. Derived coordinates
also carry `trajectory_id_origin="source_pointer_sha256"`; native locations omit it. A turn derives only
from ordered user steps within that trajectory (`ordered_atif_user_step`); otherwise it is
unknown. `event_index` remains the zero-based portable-record index. Traversal order across
concurrent subagents does not establish causality.

```python
# atif_document is already loaded JSON; requirements is the actual execution policy.
trace = workspace.upload_atif(atif_document, requirements, agent_version="build-42")
review = workspace.evaluate(trace["id"], idempotency_key="build42-process-review01")
```

## Client methods and effects

`KumaClient.trajectory(timeout=180)` returns an optional client with its own request timeout.
The timeout must be positive and finite seconds. Parent client configuration is unchanged.
All workspace HTTP calls disable automatic retries. No new account, model client or dependency
is added to the SDK.

| Method | Inputs and result |
| --- | --- |
| `save_requirements(requirements)` | Validated complete profile; returns `{id, kind, content_sha256, summary}`. |
| `upload(submission, agent_version=None, case_id=None, tags=None)` | Prepared submission; optional nonblank version ≤80, case ID ≤128 and ≤32 tags of 1–120 characters. Returns a trace receipt. A case ID explicitly identifies the same task across versions. |
| `evaluate(trace_id, idempotency_key=..., limits=None)` | Owned trace ID and explicit 8–200 character logical request key (`A–Z a–z 0–9 . _ : -`, first character alphanumeric). Optional integer budgets: `decisions` 1–12 (default 6 provider calls), `seconds` 1–900 (default 120 wall-clock seconds), `source_characters` 1000–4194304 (default 4194304 unique source characters), `chunk_characters` 1000–350000 (default 240000 primary source characters per call, including task/capture anchors). Legacy `tool_calls` 1–24/default 12 and `evidence_characters` 1000–120000/default 40000 continue to bound optional read tools; the process driver uses direct source windows. Deployment timeout/lease constraints can reduce accepted seconds. Returns a completed report or explicit stopped artifact. |
| `run_experiment(name, dataset_id, idempotency_key=..., limits=None)` | Nonblank name ≤200, owned frozen dataset, explicit run key and optional per-trace budgets. Sequentially reviews ≤50 traces; deterministic child keys recover previous outcomes on explicit resume. Returns `{experiment, reviews, errors}`. Experiment is null when no review artifact exists; errors contain only trace ID and safe category. No Agent execution or ranking. |
| `export(kind, identity)` | One of requirements/traces/reviews/datasets/experiments/feedback and an owned ID. Returns hash-verified JSON bytes ≤6 MiB, preserving original numeric values. Caller owns saving. |
| `request(action, data, idempotency_key=None)` | Explicit action below and finite action data. Returns the detached result. Review keys are mandatory; other calls use a fresh transport request ID if omitted. No automatic pagination. |

Local errors raise `ValidationError` before network use. Missing credentials raise
`AuthenticationError`; malformed remote responses raise `ProviderError`. Auth, scope, credit,
idempotency, timeout and availability failures use existing `KumaError` categories. Error messages
do not echo source/credentials. A timeout is an unknown server outcome: retry explicitly with
the **same key and input**, not a new key. A deliberate new paid review needs a new key.

Reads require `sdk:read`; save/import/dataset/experiment/feedback writes require `sdk:observe`;
reviews require `judge:run`. Existing keys are not silently granted new scopes. Only completed,
validated reviews settle the configured existing Judge fixed fee in public Credits. Storage,
imports, reads and human feedback do not invoke Judge or consume that fee. Internal accounting
continues using existing integer units; the SDK never multiplies your displayed balance.

## Consume an experiment result

On normal return, `run_experiment` always returns a dictionary with exactly
`experiment`, `reviews`, and `errors`; never test `result is None`.
Only `result["experiment"]` can be None. It is None when no evaluation returned
a review artifact, in which case no experiment write is attempted.

| Outcome | experiment | reviews | errors |
| --- | --- | --- | --- |
| Every evaluation returns an artifact | Saved receipt dict | All returned artifacts | Empty list |
| Some evaluations raise KumaError | Saved receipt dict | Returned artifacts in dataset order | Failed trace IDs and safe codes, in dataset order |
| Every evaluation raises KumaError | None | Empty list | One trace_id/code entry for each failure |

A stopped review is still an artifact in `reviews`, not an entry in `errors`.
Neither a saved experiment nor an empty error list proves Agent success.
Initial dataset read/validation errors propagate before any evaluation. Final
experiment-save errors also propagate, after reviews may already have completed
and incurred fees; there is no returned wrapper on those failures. Invalid
name/key, unsafe child-key seed and cancellation are not converted into per-item
errors. Only `KumaError` raised by individual `evaluate` calls is accumulated.
Explicit recovery must keep the same name, dataset, limits and run key.

The offline [consumer example](../examples/consume_experiment.py) demonstrates
all-completed, partial-failure (including stopped), and all-failed wrappers:

```sh
python examples/consume_experiment.py
```

It uses clearly synthetic display-only data, not fake evidence of service calls.
For a real call, catch `KumaError` around `workspace.run_experiment(...)`;
handle a propagated failure separately from a returned wrapper. Inspect safe
error codes rather than logging raw exceptions, credentials or full report bodies.
See the [API reference](api-reference.md#trajectoryclientrun_experiment) for the
complete argument, exception and side-effect contract.

## Action data

| Actions | Exact data |
| --- | --- |
| `requirements.save` | `{requirements}`. |
| `traces.import` | SDK uses `{submission, agent_version, case_id, tags}`. Browser may instead supply `{source, requirements_id, agent_version, case_id, tags}` with raw ordered events/native bundle and a saved requirements ID. Prepared source already binds requirements, so its requirements ID must be null/omitted. |
| `<kind>.get`, `<kind>.export` | `{id}` for any of the six categories. |
| `<kind>.list` | Optional `limit` (1–100, default 20), `cursor` (null or ≤4096), `query` (name substring ≤200), `task_type` (exact ≤120), `agent_version` (exact ≤80), `tag` (exact ≤120). Unknown fields are rejected. Returns metadata-only `items` and nullable `next_cursor`. Writes commit synchronously; listing reads committed, owner-bound documents with stable keyset pagination and the supplied kind/filters. Pass the returned cursor unchanged with the same filters. |
| `datasets.create` | `{name, trace_ids}`; nonblank name ≤200 and 1–50 unique owned IDs. Immutable ordered snapshot. |
| `reviews.run` | `{trace_id, limits?}`; normally call `evaluate`. |
| `experiments.create` | `{name, dataset_id, review_ids}`; 1–50 unique owned reviews from that dataset. Missing/stopped cases remain visible. |
| `experiments.compare` | `{baseline_id, candidate_id}`. Only unique matching case IDs with identical requirements/Judge revisions and completed reviews are compared. Reports gaps and category deltas; no overall winner or retry penalty. |
| `feedback.submit` | `{review_id, labels}`. Start from the report's blank `process_labels`; inspect evidence, fill each `expected` and set `label_source="human"`. Backend records the authenticated actor. This is a human declaration, not certified independence. |

Details add `payload` to a receipt. Completed review results additionally contain `trace_id`,
`review_status`, `reason`, `attempts`, `report`, `process_labels`, `judge_version`,
`requirements_sha256`, `case_id`, `agent_version`. Attempts contain serving provider/model,
actual token usage, latency and attempt count; no private instructions or credentials.
Stopped work has no successful report and must never become a failing Agent score.

Reports separate calling issues, improvement opportunities, reasonable retries, useful actions
and insufficient evidence. They preserve citations, zero-based event indices, recorded turn
locations and conservative later-recovery explanations. Numeric scores and automatic penalties
are not generated by this workflow. Human disagreement counts cover only reviewed claims;
they do not establish overall accuracy or prove that no issue was missed.

### Process report and delivery coverage

Formal process reviews return `kuma.quality_agent_report.v4`; saved v3 reports remain readable.
The result also contains `coverage`, identical to `report.coverage` for completed v4 reports.
Stopped attempts retain coverage and actual usage with `report=null`; they are not completed
reports and do not settle the fixed fee. The source budget counts Unicode characters, not bytes
or tokens. A separate two-MiB serialized model-input ceiling applies to every call.

Every source is partitioned into exact ranges with at most 800 characters of overlap, with the
task/capture anchors repeated in each primary batch. A small earlier exact excerpt may provide
continuity; no generated summary replaces source text. At most two calls per segment (including
one repair) share the original decision/time budget. If planned segments exceed available calls,
review stops before a paid call. Any unaccepted segment prevents a completed report. No source
is silently clipped to satisfy a budget. Choosing smaller chunks may require more calls.

`coverage` has schema `kuma.process_coverage.v1`:

| Field | Meaning |
| --- | --- |
| `sources` | Map keyed by source ID. Each has nonnegative `characters`, merged `delivered_ranges` and complementary `uncovered_ranges` as half-open `[start,end]` character intervals, plus boolean `fully_delivered`. Offsets refer to that exact source text. |
| `total_source_characters`, `delivered_source_characters` | Unique source-character counts; overlap and repeated anchors count once. |
| `source_delivery_complete` | All source ranges were sent in answered provider calls. This does not prove the model attended to each detail. |
| `all_segments_accepted` | Every planned fragment passed schema, citation and controller checks. |
| `segments` | Ordered records with zero-based `id`, status `not_started`/`completed`/`rejected`/`provider_failed`, planned `source_ranges` (`id`,`offset`,`end`) and `calls` (0–2 answered calls). Provider errors may lack returned token usage. |
| `semantic_recall_certified`, `delivery_is_attention` | Always false. Delivery and accepted output do not certify correctness, attention or complete issue recall. |

The report includes `segments` with each original `raw_assessment`, cumulative `finding_start`
and `finding_count`. Local ledger entries carry `segment_id`; annotations also retain
`segment_finding_index` and use global finding indices. Up to 12 findings per accepted segment
are retained (at most 144), without silently dropping findings after the first twelve. Overall
outcome stays unknown and all numeric dimension scores remain null. Incident counts use explicit
operation identities when available; classification counts are findings, not independent failures.

### Evidence-limited final wording and raw audit

If a final process classification is `unknown`, its final title, explanation and rationale say
that the available evidence cannot confirm the action. An Agent self-report without a matching
successful result is not presented as completed work. Original citations, locations and
`proposed_classification` remain available. Each report segment additionally contains
`unconfirmed_model_output` with exactly `status="unconfirmed"` and `process_draft` (the complete
original accepted model draft). The latter, `raw_assessment`, adjustments and audit are raw
provenance; they may include claims the controller withheld. Display final findings/rationales
as conclusions and label original audit text as unconfirmed. No semantic precision/recall or
independent reproduction is certified. Older saved v3 reports retain their historical semantics.

Formal persistence uses Core's existing private SQL database with bounded storage admission; no
Langfuse account/key is required. Capacity failure preserves data and does not settle a Judge fee;
public error projection uses existing availability categories, with no automatic paid retry.
