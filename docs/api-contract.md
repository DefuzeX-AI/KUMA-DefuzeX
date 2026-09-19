# KUMA SDK API Contract

For active operation intervals, bounded backoff and strict timeouts, see
[Operation polling and deadlines](operation-polling.md).

Base URL: `https://defuzex.ai/api/agentdefuze`

## Saved Cases and original official Judge inputs

`Run.save_case(path)` / `create_run(case_path=...)` use closed local
`kuma.case_artifact.v1`. See [Case files](case-files.md) for complete fields,
5 MiB/depth limits and provenance rules. Official Judge uploads the full
ten-field public Case in raw schema `2`, using part `case_file`, filename
`kuma-official-case.json`, MIME `application/json`. It sends neither the local
artifact wrapper nor an additional top-level `case_id`. Existing metadata
`repo_fingerprint/case_sha256/case_signature` must match the raw Case. Batch
entries reference their Case part through `case_file_part`. Case bytes count
against existing dynamic per-file and total limits. Custom Cases continue to
use `defuzex.custom_case.v1`.

A public checksum is not authentication. Before a new reservation, Backend
verifies the tenant's original Case and the originating CaseGen control's unique
Rubric reference. SDK neither imports Rubrics nor downgrades edited official
Cases to custom ones, and never falls back to case-ID-only submission when an
older Backend rejects the upload. Historical reference paths are not implicit
fallbacks for the new save/load workflow.

Official Case POST optionally includes top-level `tool_capabilities` using
`kuma.agent_tool_capabilities.v1`: exactly `schema_version/provenance/tools`;
each tool has exactly
`name/version/input_schema/read_only/side_effects/resource_scopes/evidence_types`.
Only an explicitly linked Agent Profile file causes the complete normalized
document, including schema descriptions/defaults/examples, to be sent. Omission
means undeclared; wire null is invalid. Local paths are never sent; Judge wire
gains no field. The declaration is context for the selected Group, not Evidence
or a new selection signal.

The limit is 262144 UTF-8 bytes measured as JSON with `ensure_ascii=False,
indent=2, sort_keys=True, allow_nan=False` plus a final newline. Root depth is 0,
maximum depth 32, tool count 1–100. Schemas are not truncated or edited. The full
document participates in request hash/idempotency; omission preserves original
identity. Invalid format/size/reference yields `tool_capabilities_invalid`;
sensitive/private content yields `sensitive_data_blocked` before networking,
even with allow_sensitive=True. Unsupported Backend errors are returned rather
than dropping the field and retrying; there is no extra capability-probe GET.
See [Agent tool capabilities](agent-tool-capabilities.md).

All URLs have trailing slashes. Requests use:

```http
Authorization: Bearer dfx_<public-id>.<secret>
Accept: application/json
```

## Entitlements

`GET /sdk/entitlements/`

Official Case `max_steps` is an upper bound, not an exact count. Without a pending
operation, SDK reads entitlements; an explicit value above the server limit
fails with `case_step_limit_exceeded` before creating an operation, with safe
`details.max_allowed_steps`. No silent clamping occurs. Backend defensively
enforces the same bound before billing or Core calls. Existing pending operations
skip repeat preflight and resume the same operation, preserving replay across
configuration changes. Omission uses the server default; under the current v1
contract, omission and explicit 10 share wire/request identity. Success contains
1..requested-limit complete steps; Case truncation is forbidden.

For direct `OfficialCaseProvider.generate_case(context)`, context.max_steps is
authoritative for wire and result validation. An explicit constructor limit must
match it, or SDK rejects before any GET/POST. Entitlements return user ID, API-key
metadata, scopes, subscription and weekly quota, never full API keys or hashes.

## Strategy Group catalog and selection

Strategy Group leads tested capability, domain and method. Agent Profile provides
Agent/scenario/expected-behavior/prohibited-boundary context; its prose does not
infer, replace or override a Group. Explicit strategy_group coordinates take
priority; otherwise use catalog default. Automatic capability matching is
disabled in the current SDK; scan_strategy_group=True is rejected.

`GET /sdk/strategies/` returns closed `kuma.strategy_group_catalog.v1`: a
64-character lowercase hexadecimal catalog_release, exact default:{id,version},
limits:{max_selected_groups:1}, and 1–128 groups ordered by(id,version). Each group
contains only id/version/display_name/description/required_capabilities/available/
limits. Group limits contain max_steps:1..10 and a canonical D0–D2 subset. The
semantic General group referenced by default must be available and require no
capabilities; its ID is not hardcoded as general.

Every official Case request with the new catalog, including judge=False, sends:

```json
{
  "strategy_group_selection": {
    "schema_version": "kuma.strategy_group_selection.v1",
    "strategy_group_id": "basic-safety-general",
    "strategy_group_version": "1",
    "selection_source": "user|scanner|general",
    "catalog_release": "<64 lowercase hex>"
  }
}
```

This illustrates field shape, not a guaranteed deployed coordinate;
selection_source is one enum value, not the literal pipe-separated string.
Agent Profile front matter permits only {schema_version,id,version}. Explicit
unknown/unavailable coordinates or missing required capabilities fail closed
without fallback. No declaration uses catalog default. The historical scanner
wire value remains recognizable, but current automatic selection is disabled.
Older Backend compatibility retains old Case wire only without explicit Group
declaration; otherwise SDK raises strategy_group_unsupported.

## Error semantics

- 401: missing/malformed/invalid/expired/revoked key.
- 403: user/subscription/scope does not permit the operation.
- 429: exhausted account quota.

## Protected services

- cases:generate: Case generation.
- judge:run: LLM-as-Judge.

These inherit Backend API-key authentication, subscription, scope and quota
permissions and require idempotency keys. SDK accepts real service results only,
without a simulated-success fallback.

## Custom Case Provider result

Supported results are Case, a Case mapping with required inputs, one string, one
KumaInput, or a list/tuple of strings, structured JSON mappings or KumaInput.
A top-level mapping is a Case envelope: missing inputs raises
ProviderError(code="provider_failed"), not an arbitrary structured-Input fallback.
Generators and arbitrary iterables are outside the contract.

Before constructing the first Input, SDK recursively scans Case, Input payload,
public constraints and extensions. Evaluation fields such as rubric,
private_rubric, rubric_context, answer_key and expected_output fail closed;
their values must not enter errors, History, Evidence or official Judge wire.
Custom Cases carry no Rubric. Custom Judge receives public Case/History/Evidence.
Official Judge's custom multipart uses closed defuzex.custom_case.v1 with Case ID,
input type/schema and public Inputs only; it sends no rubric_id/case_revision_id
and derives no criteria.

## Input and Submission JSON graphs

Structured KumaInput.payload and Run.submit(output) accept finite JSON graphs:
null, text, booleans, integers, finite floats, mappings and lists/tuples. Root
container depth is 1, maximum 256. Depth 257 fails before Input/Submission
construction, Evidence preparation, persistence or public requests. Self/mutual
mapping/list cycles fail; shared acyclic children remain valid.

Run/Input raises ValidationError(code="output_invalid") without keys, values,
repr, raw exceptions or host information. Equivalent custom Provider failures
remain safe ProviderError(code="provider_failed"). NaN, positive/negative Infinity,
sets, bytes-like values and unsupported custom Sequences are invalid. Custom
Mappings must be safely and finitely traversable.

### Frozen public contract export

Public objects freeze mappings against post-submission mutation. Before writing
JSON or using an encoder:

```python
import json

import kuma

plain = kuma.to_json(run.history[-1].submission)
encoded = json.dumps(plain, ensure_ascii=False, allow_nan=False)
round_tripped = json.loads(encoded)
```

kuma.to_json(value) accepts public contracts or their individual public JSON
fields and returns a detached plain graph, not text. It preserves field names
and values for KumaInput.payload/public_constraints/extensions,
Submission.output/logs/extensions, Case.inputs/input_schema/extensions,
HistoryItem, TestReport.issues/evidence_gaps/extensions and public File Evidence/
Capture Status. Failed JudgeBatchResult projects only stable KumaError public
fields code/message/request_id/retryable/details. StrategyGroupDeclaration,
StrategyGroup, StrategyGroupCatalog, ResolvedStrategyGroup, ResourceScope,
ToolCapability and AgentCapabilities reuse canonical to_dict()/wire projection;
nested group limits are projected through the parent, not a second definition.

The same graph rules apply: maximum 256 user-container levels, acyclic aliases
allowed, cycles/nonfinite numbers/unsupported objects rejected with output_invalid.
Arbitrary dataclasses are not reflected; errors contain no source values/repr.
New containers share no mutable source state. Conversion has no file, Evidence,
network, billing or Run-state side effects and changes no public wire.

## Run.submit log paths

logs accepts None or an ordered sequence, normally list/tuple, of strings or
os.PathLike[str]. Bare strings, bytes, bytearray, generators, unordered sets and
nontext paths raise ValidationError(code="logs_invalid") before Evidence reads
or Judge transport; the current Input remains retryable.

Relative paths resolve from the Run's canonical repo_path, not cwd. Absolute
paths must remain inside it. POSIX ../absolute escapes, other Windows drives/UNC
spellings and symlink/reparse/mount components degrade to safe
`log_path_outside_root:<index>`, `log_path_symlink:<index>` or
`log_path_mount_boundary:<index>` without reading outside targets. Missing,
unreadable, invalid-suffix, binary, count and byte-limit failures also use safe
index reasons. Successful paths are repo-relative POSIX strings, preventing
host absolute paths in errors, History, local Evidence and official Judge wire.
Existing content limits/scanning remain; this does not broaden allow_sensitive.

The canonical high-confidence pretransport scanner covers Agent Profile,
Submission output/error, explicit logs and custom Cases. Bare OpenAI classic/
project and Anthropic sk- keys use stable rule sk_api_key; only rule/safe-location
metadata is retained, never matched values in errors, History, Evidence or wire.
It recognizes prefixes rather than guessing general entropy.

## Official Case/Judge v2 operations

- `POST /sdk/v2/cases/generate/`
- `POST /sdk/v2/judge/`

Accepted requests and idempotent replays return HTTP 202:

```json
{"operation_id":"...","status":"queued","poll_after_ms":1000}
```

Status is queued/running/succeeded/failed; poll_after_ms is authoritative
100..60000 milliseconds. Retries preserve Idempotency-Key and exact body, without
v1 fallback. GET /sdk/v2/operations/{operation_id}/ retrieves state. Active
wrappers carry operation_id/status; success adds existing Case/Judgment result;
failure adds error:{code,retryable,message?}. Message is optional frozen public
text, omitted by older servers. Unknown operations return HTTP 404
operation_not_found; failed operations themselves use HTTP 200 wrappers.

Each new start also sends X-Kuma-Client-Request-Id: kreq_<32 lowercase hex>.
Retries preserve that ID, idempotency key and body byte-for-byte. Recovery uses:

```text
GET /sdk/requests/{client_request_id}/
```

Closed kuma.request_recovery.v1 contains only schema_version/client_request_id/
request_type/operation_id/status. Ownership binds creating tenant/user, exact
API key and scope; unknown/cross-key/unauthorized records all return
operation_not_found. Known operation IDs use existing operation GETs only.

HTTP timeout and operation_wait_timeout are independent. Before first POST,
SDK atomically writes .kuma/requests/<client_request_id>.json under repo root:
8 KiB record limit, 4096 directory-entry scan limit, fail-closed symlink/reparse/
mount boundaries. Records contain operation type/ID, idempotency key, SHA-256
request/Backend/API-key fingerprints, Run/Case association, times and public
report locator; never keys, headers, request/Evidence/Rubric/provider bodies or
raw remote errors. Terminal records remain.

High-level retry GETs known operations only. Before receiving an ID, original
high-level material can validate the hash and replay the same POST. Standalone
resume_request has no saved body, so it looks up first; lookup 404 raises
request_not_started, never an empty POST. Successful public Judge reports are
saved at .kuma/reports/<run_id>.json with a 1 MiB limit. Recovery can finish an
accepted operation after process exit but cannot reconstruct an Input-executing
Run/History from run_id alone. POST /sdk/judge/batch/ remains synchronous.
