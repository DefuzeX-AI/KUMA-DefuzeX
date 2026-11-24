# KUMA Python API reference

For explicit `KumaClient.upload_observation`, `list_observations`,
`get_observation` and `delete_observation`, including arguments, return fields,
scopes and privacy, see [Cloud observations](cloud-observations.md).
These APIs are available in KUMA 0.3.0 or later; cloud features also require server support.

For active operation interval revisions, bounded backoff and strict timeout
behavior, see [Operation polling and deadlines](operation-polling.md).

[Chinese overview](../README.zh-CN.md) | English

This page documents the stable user-facing Python entry points. Types, defaults,
ranges, side effects, and failure behavior match the current implementation.
KUMA uses keyword-only arguments for its main APIs so call sites remain readable.

## Normal execution capture (0.5.0)

`capture(task, ...)` binds the original task before normal Agent execution and automatically
persists redacted local evidence. `list_captures`, `export_capture` and `judge_capture` use
durable IDs, including installed `kuma captures` commands. No ATIF conversion or separate
rules file is required for basic use. See [capture lifecycle](capture-lifecycle.md) for every
parameter, integration/storage/exception contract, report recovery and CLI option.

## Existing execution review (0.4.0)

`KumaClient.trajectory(timeout=180)` reuses the existing Kuma credential for explicit trace
upload and process review. See [Trajectory Judge](trajectory-platform.md) for every parameter,
requirements field, permission, paid-call/idempotency behavior and report/coverage/audit field.
No separate model or storage key is required. This does not rerun an Agent or generate Cases.

## Local observation: `observe` and `ObservationSession`

Use `kuma.observe(tracer_provider=None, external_run_id=None,
external_invocation_id=None, limits=None)` as a synchronous or asynchronous context
around an already instrumented Agent. It creates no Case/Judge, reads no credentials
and performs no upload. After exit, call `export()` for detached redacted JSON,
`render_text()` for a timeline, or `save(path, root=...)` for explicit atomic local
publication without overwriting existing files. Capture failures do not block the
Agent; an Agent exception is propagated unchanged.

See [Local observation](observation.md) for every parameter, the complete export
schema, lifecycle/async behavior, limits, privacy, exceptions and a runnable example.

## `check_for_updates`

```python
from kuma import check_for_updates

update = check_for_updates()
print(update["status"], update["latest_version"], update["release_url"])
```

No parameters. Returns a detached dict: `status` is `disabled`, `checking`,
`unavailable`, `up_to_date`, `optional` (new patch) or `required` (new major/minor);
`current_version` is the local string, `latest_version` and `release_url` are
validated official-release values or `None`, and `cached` is boolean. Required
is a strong reminder, never a blocked request or automatic installation.

Explicit calls can perform one anonymous GitHub HTTPS request (one-second socket
timeout, 64 KiB cap, no retry); ordinary failure returns unavailable. Concurrent
checks return checking without waiting. Success/failure is cached in memory for
24 hours per process. Set `KUMA_DISABLE_UPDATE_CHECK=1` to disable even explicit
calls. No credentials, Agent data, disk writes, proxies or redirects. Official
transport schedules the same checker on a daemon thread; import/help/local/custom
paths do not check. CLI equivalent: `kuma updates check`, JSON stdout, exit zero.
See [release policy](releases.md) for all status fields and lifecycle limits.

## `configure`

```python
from kuma import configure

credential_path = configure(api_key="dfx_your_key_here")
```

<!-- api-parameters:configure:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `api_key` | `str` | Required | Saves the credential KUMA will use for official Case and Judge requests. Copy the `dfx_...` value issued to you; it must be printable ASCII, contain no whitespace/control characters, and be at most 512 encoded bytes. You do not need this for fully local runs. |

<!-- api-parameters:configure:end -->

**Returns:** the absolute `Path` of the atomically written user credential file.

**Preconditions:** pass the complete platform-issued `dfx_...` value. If
`KUMA_CONFIG_HOME` is set, it must name a directory this process may use for the
credential file.

**Postconditions:** on success, the returned file exists and contains the
validated key. Atomic replacement prevents a partially written final file; a
failed write removes its temporary file.

**Raises:** `ConfigurationError` for invalid keys, unresolved credential locations
or mapped filesystem failures. Public errors do not expose raw OS paths/causes.

**Side effects and security:** creates the credential directory if needed but
makes no network request. The file contains the real key; never print, upload,
or commit it.

## `create_run`

```python
from kuma import create_run

run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
)
```

<!-- api-parameters:create_run:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `repo_path` | `str \| os.PathLike[str]` | `"."` | Chooses the repository being tested. KUMA reads bounded metadata and, when enabled, observes file changes below this directory. Use `"."` when your Python process already runs at the repository root. |
| `storage_path` | `str \| os.PathLike[str] \| None` | `None` | Existing external artifact directory, absolute or relative to repo_path. Must not overlap repo_path. None preserves repo/.kuma; explicit mode stores Run state and ledgers under storage_path/.kuma without writing workspace .kuma or .gitignore. Saved Case paths are contained in this selected root. Invalid/unsafe roots raise ConfigurationError before external work, never fall back. This is not sandbox isolation; see the SDK guide. |
| `agent_profile_path` | `str \| os.PathLike[str] \| None` | `None` | Points to the UTF-8 Agent Profile describing the Agent, production scenario, expected behavior, and prohibited boundaries. It gives context to the selected Strategy Group but never selects or overrides it. Supply it for official Case generation. Front matter may contain the only explicit group selector—a closed `strategy_group` coordinate—and a relative `tool_capabilities` file; both are validated before Provider I/O. Omit when loading `case_path` or when a custom Case Provider declares `agent_profile_required=False`. |
| `case_path` | `str \| os.PathLike[str] \| None` | `None` | Loads a complete saved `kuma.case_artifact.v1` instead of generating a Case. Relative and absolute paths are contained in storage_path when set, otherwise repo_path; never cwd. Cannot combine with `case_provider`, `agent_profile_path`, or non-`auto` strategy. Loading validates at most 5 MiB before credentials/runtime setup and makes no CaseGen/catalog call; `max_steps=None` uses the saved count, a smaller explicit limit fails without truncation. Official Judge still needs credentials and validates the server original. |
| `case_provider` | `CaseProvider \| callable \| None` | `None` | Chooses who creates the test Inputs. Leave `None` to request an official Case from KUMA; pass a callable when your application supplies its own local Case. |
| `judge_provider` | `JudgeProvider \| callable \| None` | `None` | Chooses who evaluates all submitted results and builds the final report. Leave `None` for the official Judge, or pass a callable for your own local evaluation. Ignored when `judge=False`. |
| `strategy` | `str` | `"auto"` | `auto` uses the catalog's exact default unless a Profile group is explicit. `safety-baseline` uniformly samples one of seven validated Basic Safety groups for one Case; an explicit Profile group wins. Custom providers receive the value unchanged. Other strategy ID validation is unchanged. See [selection and recovery boundaries](strategy-groups.md#sample-one-basic-safety-group). |
| `max_steps` | `int \| None` | `None` | Limits how many test steps this Run may contain. For example, `3` allows one, two, or three steps—it does not force exactly three. `None` uses the official service limit; custom Case Providers require an explicit positive value. An explicit official value above the advertised limit fails before Case generation, and KUMA never truncates a returned Case. |
| `difficulty` | `str` | `"D1"` | Official generation challenge level: `"D0"` injects zero problems; `"D1"` injects one obvious, low-intensity problem; `"D2"` injects two subtler or composed problems requiring stronger recognition, recovery and verification. `max_steps` remains an upper bound, not an extra-step allowance. Necessary inputs and solvability must be preserved. No measured failure rate or Judge-severity change is promised. Other values including `None` raise `ConfigurationError(code="config_invalid")` before I/O. Default/explicit D1 omit the wire field; D0/D2 are included in request identity. Custom providers and loaded Cases are unchanged. |
| `high_difficulty` | `bool` | `False` | Explicit official high-challenge generation intent requiring service support and permission. False preserves ordinary difficulty behavior and wire omission; True participates in request identity without changing Judge scoring. Custom and loaded Cases do not generate or receive this flag. |
| `judge` | `bool` | `True` | Controls whether KUMA evaluates the Run after the last Input. Keep `True` to receive a `TestReport`; use `False` when you only want to execute and record the Case, in which case `run.report` remains `None`. |
| `on_failure` | `str` | `"continue"` | Decides what happens after you submit a step as `failed`, `timeout`, or `aborted`. `"continue"` delivers the next Input; `"stop"` ends the Run immediately. |
| `allow_local` | `bool` | `False` | Allows the Run to start outside Docker for trusted local development. It only bypasses the Docker execution constraint: it does not sandbox the Agent, expand file access, or weaken validation and privacy checks. |
| `track_files` | `bool` | `True` | Tells KUMA to compare repository file metadata before and after each Input so the Judge can see which files were created, modified, deleted, or renamed. Set `False` when file changes are irrelevant or unavailable. |
| `upload_diff` | `bool` | `False` | Adds bounded changed text to file Evidence instead of sending only paths, hashes, sizes, and change types. Enable only when the Judge needs the actual diff and the repository text is safe to disclose; requires `track_files=True`. |
| `save_local` | `bool` | `False` | Writes a local JSON copy of each committed Submission under `.kuma/runs/<run_id>/`. Use it for debugging or audit records. It does not replace submission to an official Judge. |
| `allow_sensitive` | `bool` | `False` | Lets ordinary Evidence continue when KUMA's scanner flags content as potentially sensitive. Leave `False` unless you reviewed that content and intend to disclose it; this never allows secrets into OTel Trace Evidence. |
| `timeout` | `float` | `300.0` seconds | Limits one HTTP connection attempt to the public KUMA service. Lower it to fail individual network calls sooner. It does not limit the total time spent waiting for Case generation or Judge completion. |
| `operation_wait_timeout` | `float` | `600.0` seconds | Limits the total synchronous wait for one official Case or Judge operation, including polling. If it expires, KUMA raises a retryable timeout and keeps safe recovery metadata so the same operation can be resumed. |
| `max_retries` | `int` | `2` | Sets how many additional attempts KUMA may make after a transient HTTP failure; accepted values are 0–5. Retries reuse the same idempotency key and do not intentionally create another Case or Judge operation. |
| `api_key` | `str \| None` | `None` | Supplies the official-service credential for this Run only. Use it to override the environment or saved credential. With `None`, KUMA checks `KUMA_API_KEY` and then the user credential file. Fully local Provider combinations need no key. |
| `trace_evidence` | `TraceEvidenceCapture \| None` | `None` | Supplies a specific in-process OTel capture and its limits for this Run. Pass the object returned by `configure_trace_evidence()` when you need explicit control. With `None`, KUMA safely reuses a compatible global Provider when available; otherwise the Run continues without Trace Evidence and records a warning. |
| `scan_strategy_group` | `bool` | `False` | Disabled automatic matching flag: keep `False`. `True` raises `ConfigurationError(config_invalid)` before file or network I/O, including with an explicit group or custom provider. Privacy scanning and Evidence capability validation remain enabled. |
| `external_run_id` | `str \| None` | `None` | Optional non-secret caller execution label: 1–128 ASCII characters, starting with a letter or digit, then letters, digits, dot, underscore, colon or hyphen. Enables negotiated Official Judge correlation; unsupported services reject before Judge POST instead of dropping it. Omission preserves existing wire unless a Submission supplies an invocation label. |
| `case_request_id` | `str \| None` | `None` | Persist one `kreq_` plus 32 lowercase hex digits per logical official generation. Reuse on recovery; distinct IDs intentionally create independent requests. None preserves legacy matching. Unsupported with custom/reloaded Cases. Binding conflicts prevent POST; read-only discovery can precede payload comparison. Older readers may reject explicit ledger records. |

<!-- api-parameters:create_run:end -->

`external_run_id=None` optionally labels the caller's Agent execution. Use a
non-secret ASCII label of 1-128 characters (`A-Z`, `a-z`, digits, `.`, `_`, `:`,
`-`, starting with a letter or digit). It enables negotiated Run correlation;
unsupported official services reject before Judge POST rather than dropping it.
See [Run correlation and stage timings](run-correlation.md).

**Returns:** a synchronous `Run` in `ready` state.

**Preconditions:** `repo_path` identifies the repository the caller authorizes
KUMA to inspect. Official Case generation needs a readable Agent Profile and a
valid key. Unless `allow_local=True`, execution must be inside the supported
container environment. Only one Run may own the local active-Run lock.

**Postconditions:** the returned Run owns that lock and contains one validated
Case. If `max_steps=N`, the Case contains from 1 through N Inputs, not exactly N.
If setup fails after runtime acquisition, KUMA closes the runtime and releases
the lock before re-raising the error.

**Raises:** configuration, credential, isolation, Provider, Case, or public
service failures raise a concrete `KumaError` subclass with stable `code`,
`retryable`, and optional `request_id`.

**Side effects and security:** reads the Agent Profile and bounded repository
metadata, may create `.kuma/`, and may call only the public Backend for official
Providers. It never contacts MCP, a model, or a database directly. Custom
Providers run in the caller's process with that process's permissions.

### Official generator selection

`create_run(..., high_difficulty=False)` accepts only a boolean. The default or
explicit `False` retains the default service generator and is omitted from the
wire/hash. `True` requests the service-managed alternate generator and participates
in request/recovery identity. It is independent of D0/D1/D2 and does not change
`max_steps`, Judge, custom providers or loaded Cases. Non-booleans fail before I/O
with `ConfigurationError(code="config_invalid")`. Service failures never cause
fallback to the default generator.

Direct callers use `OfficialCaseProvider(client, high_difficulty=True)` and
then `.generate_case(context)`. Retry with the same flag to recover the original
operation; changing it creates a different request identity. The SDK accepts no
model credentials or provider configuration through this option.

## Agent Profile parsing

```python
from kuma.repository import AgentProfileSpec, parse_agent_profile

profile = parse_agent_profile("agent-profile.md")
```

<!-- api-parameters:parse_agent_profile:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `path` | `str \| Path` | Required | Selects the UTF-8 Markdown Agent Profile to validate. Pass the exact file used for this Run; relative paths resolve from the process working directory, so applications should prefer an explicit repository-relative or absolute path. |

<!-- api-parameters:parse_agent_profile:end -->

`parse_agent_profile(path)` reads the explicitly selected UTF-8 Markdown file,
accepts an optional leading BOM, validates closed front matter and the three
required behavior sections, and returns an immutable `AgentProfileSpec`. It may
also read one explicitly linked relative input schema and one tool-capability
file contained by the Profile directory. It performs no network request and does
not upload the raw Profile. Missing files use `agent_profile_required`; malformed
content uses `agent_profile_invalid`. The returned `strategy_group` is an exact
coordinate declaration—not an inference from Profile prose.

## `Run`

### `save_case`

<!-- api-parameters:save_case:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `path` | `str \| os.PathLike[str]` | Required | Destination within this Run's repository; relative paths are repository-relative. Its parent must already exist. Choose a new filename: existing files, symlinks, mount escapes and concurrent overwrites are rejected. |

<!-- api-parameters:save_case:end -->

**Returns:** absolute `Path` to the complete UTF-8 Case artifact (maximum
5,242,880 bytes). **Preconditions:** the Run has a validated complete Case;
official Cases must retain their original public record. **Postconditions:**
Run state, history and Input position do not change; no runtime ID, Evidence,
Agent output, Rubric or credential is saved. **Raises:**
`ValidationError(case_artifact_invalid)` for invalid/changed content,
`ValidationError(case_origin_invalid)` for conflicting origin,
`SensitiveDataError` for sensitive/private content, and `ConfigurationError`
for unsafe/unwritable/existing paths. **Side effects:** bounded atomic file
publication, no network. Privacy checks cannot be disabled. Public checksums
detect corruption, not authenticity; official Judge validates the tenant-owned
server original. [Save/load example and wire boundaries](case-files.md).

`run.case_origin` is read-only `"official" | "custom"`; it describes the Case,
not which Judge is configured. An official Judge does not make a custom Case official.

### `get_input`

<!-- api-parameters:get_input:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `full` | `bool` | `False` | Chooses how much information your Agent receives. Keep `False` to get only the actual task payload. Use `True` when your integration also needs identifiers, index, payload type, constraints, or extensions from the immutable `KumaInput`. |

<!-- api-parameters:get_input:end -->

**Returns:** the current payload or immutable `KumaInput`; returns `None` after
all Inputs are committed.

**Preconditions:** the Run is `ready` or already `input_delivered`; submit the
current Input before requesting a different one.

**Postconditions:** first delivery changes `ready` to `input_delivered` and
starts step Evidence. Repeated calls return the same Input without advancing
state or history.

**Raises and side effects:** invalid order raises `InputProtocolError`; Evidence
startup may raise `EvidenceCaptureError`. The method may begin bounded capture,
but it never calls Judge or appends history.

### `submit`

<!-- api-parameters:submit:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `output` | finite JSON-compatible value | Omitted | Sends the Agent's result for the current Input—the value the Judge will evaluate. Pass it explicitly in normal integrations. It may be omitted only when supported OTel instrumentation captured a real final Agent/Workflow output; explicit `None` does not count as success. |
| `status` | `str` | `"completed"` | Records how the current Input ended. Use `"completed"` for a usable result, `"failed"` for an Agent error, `"timeout"` when its deadline expired, or `"aborted"` when execution was intentionally stopped. This value also drives `on_failure`. |
| `error` | `str \| None` | `None` | Provides a short, user-safe explanation when `status` is not `"completed"`. It becomes part of the Submission Evidence, so summarize the failure without secrets, file contents, or raw tracebacks. |
| `logs` | `list[str \| Path] \| None` | `None` | Names local log files whose newly appended bytes should accompany this Submission. KUMA reads only a bounded increment and applies path and sensitive-data checks. Leave `None` when logs are not needed. |
| `wait` | `bool` | `True` | Keeps final Judge execution synchronous: the last `submit()` returns only after the report or an error is available. The current public API requires `True`; background polling is not exposed. |
| `external_invocation_id` | `str \| None` | `None` | Labels this Agent invocation using the same safe ASCII rules as `external_run_id`. Enables negotiated correlation even when the Run label is omitted; never an authorization credential. |

| `public_messages` | `Mapping[str, Any] \\| None` | `None` | Pass the bounded bundle returned by `kuma.collect_public_messages` for this Input. Preserves earlier completed public messages, excludes reasoning, redacts before local persistence, and requires negotiated assessment support for Official Judge. [Exact capture/result contract](judge-assessment.md). |
| `runtime_actors` | `Sequence[Mapping[str, str]] \| None` | `None` | List/tuple of at most 1000 closed `{trace_id, span_id, actor}` declarations for this Input's captured OTel spans. Requires `public_messages`; unknown is the default for undeclared spans. IDs are nonzero lowercase 32/16-character hex. Exact matching, no descendant inference or completeness upgrade. Missing/ambiguous/duplicate selectors fail before commit; final hashes and pointers bind before POST. See [actor workflow and retry boundaries](judge-assessment.md#attribute-captured-execution-explicitly). |

| `agent_output_actor` | `str \\| None` | `None` | Explicit producer of the entire observed final output: `target_agent`, `sdk_setup`, `external_evaluator`, `reviewer`, `cross_case_reference`, or `unknown`. Default leaves it unknown. Requires completed nonempty output, Runtime Evidence and `public_messages` (an honest empty bundle is allowed). Binds the sanitized final output hash/leaf before official upload; no inferred internal speakers, execution proof or completeness upgrade. Invalid declarations fail before commit with `agent_output_actor_invalid`. See [output attribution](judge-assessment.md#attribute-an-observed-final-output-explicitly). |

<!-- api-parameters:submit:end -->

**Returns:** `TestReport` only when the final Submission completes Judge;
otherwise `None`.

**Preconditions:** one Input is currently delivered. A completed Submission has
an explicit non-`None` output or a supported OTel-captured final output. Requested
log paths are within the configured Evidence scope.

**Postconditions:** success appends exactly one immutable history item and
commits Evidence offsets, local records, and Trace byte budget together. A
validation/preparation failure leaves the Input delivered. A final Judge failure
leaves completed history available for `judge()` retry.

**Raises:** protocol, output, serialization, or Evidence failures use the
corresponding `InputProtocolError`, `ValidationError`, or
`EvidenceCaptureError`; Judge failures retain stable `KumaError` types.

**Side effects and security:** may read bounded file/log changes, atomically save
a local Submission, and synchronously call Judge. Submitted output, errors,
logs, diffs, and Evidence may cross the public Judge boundary; never include
credentials, raw tracebacks, prompts, or unapproved file contents.

### `judge`

Upload file counts come from the Backend's `max_files` configuration: the
supported maximum Case step count × 2 (currently 10 × 2 = 20), not this Run's
actual step count. One Case artifact and every Evidence file consume a slot.
For batch Judge, this limit applies independently to each item, not their sum.
The SDK does not hardcode 20. Existing byte and privacy checks remain: Backend
enforces combined Case+Evidence bytes for every item, including custom Cases.
The SDK preserves its existing per-path checks and conservative aggregate batch
cap; this update does not add combined-byte preflight for custom single Judge.
File-count rejection occurs before the Judge POST and does not start a Judge.
Deploy the matching Backend configuration before updating clients; an older
Backend may advertise a smaller limit, which the SDK continues to respect.

<!-- api-parameters:judge:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `wait` | `bool` | `True` | Makes `judge()` wait until a final report or error is available. The public Python API is synchronous, so callers must leave this as `True`; use `operation_wait_timeout` on `create_run()` to control the maximum wait. |

<!-- api-parameters:judge:end -->

**Returns:** the validated `TestReport`; repeated calls after success return the
same report.

**Preconditions:** all Inputs have committed Submissions, the Run is `completed`,
a Judge Provider is configured, and `wait=True`.

**Postconditions:** success stores the report and changes state to
`report_ready`. Failure restores `completed`, preserving immutable history,
idempotency identity, and pending operation for retry.

**Raises and side effects:** an incomplete Run raises `InputProtocolError`,
`wait=False` raises `ConfigurationError`, and Provider/service failures retain
stable `KumaError` types. The call synchronously invokes the configured Judge;
retry does not create a second operation merely because polling failed.

### `cancel`

`cancel()` has no arguments.

**Returns:** `None`.

**Preconditions:** the Run is in a cancellable lifecycle state; a failed or
actively committing state cannot be hidden by cancellation.

**Postconditions:** an unfinished Run is `cancelled`, active Evidence is
discarded, and runtime resources plus the active-Run lock are released. Calls on
already `cancelled` or `report_ready` Runs are idempotent.

**Raises and side effects:** invalid states raise `InputProtocolError`. The call
removes validated temporary runtime files but does not submit or invoke Judge.

### Read-only properties

| Property | Type | What it tells you |
| --- | --- | --- |
| `run_id` | `str` | Identifies this execution in logs, local artifacts, and public service records. |
| `case_id` | `str` | Identifies the public Case being executed. It is safe to correlate but never exposes the private Rubric. |
| `max_steps` | `int` | Reports how many steps the generated Case actually contains. It is at least 1 and never exceeds the explicit `create_run(max_steps=...)` limit, or the service/default limit when that argument was `None`. |
| `state` | `RunState` | Shows which operation is currently legal, such as delivering an Input, submitting, judging, completed, or cancelled. |
| `executed_strategy_group` | `Mapping[str, str] \| None` | Detached read-only server-reported execution metadata: schema_version, strategy_group_id (1–80 characters), strategy_group_version (1–32), catalog_release (64 lowercase hex). Historical omission/custom Run returns None, never a request/default substitute. Reading performs no I/O. Submitted coordinates are checked before success; invalid data raises invalid_response during creation/recovery. Not an independent cryptographic proof or part of the raw Case signature/Judge wire. |
| `history` | `tuple[HistoryItem, ...]` | Contains every successfully committed Input and its matching Submission in execution order. It does not include an in-progress step. |
| `report` | `TestReport \| None` | Holds the final Judge result after state becomes `report_ready`; it stays `None` before Judgment or when `judge=False`. |
| `runtime_warnings` | `tuple[str, ...]` | Lists stable warning codes for non-fatal Evidence gaps, such as unavailable automatic Trace capture. The Run can still complete. |
| `tool_capabilities_path` | `Path \| None` | Holds the absolute local path of the capability document linked by the Agent Profile. The path is retained for caller inspection and is never uploaded. |
| `tool_capabilities_provenance` | `str \| None` | Reports `user_declared`, `scanner_generated`, or `None` for the linked local capability document. It describes origin, not verified Agent behavior. |

## `KumaClient`

Use `KumaClient` for authenticated configuration reads without opening a Run.

<!-- api-parameters:KumaClient:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `api_key` | `str \| None` | `None` | Authenticates configuration reads such as entitlements and available strategies. Pass a key only for this client, or leave `None` to use `KUMA_API_KEY` and then the saved credential. |
| `base_url` | `str` | Public KUMA URL | Chooses the public Backend that receives the client's GET requests. Ordinary users should keep the default. Remote URLs must use HTTPS; loopback HTTP is allowed for local integration, and URLs containing credentials are rejected. |
| `timeout` | `float` | `30.0` seconds | Sets how long each configuration GET may wait for a response before failing. It does not control Case/Judge operation polling. |
| `transport` | public transport callable \| `None` | `None` | Replaces real HTTP with an explicitly supplied transport callable. This is for tests or controlled integrations; ordinary applications should leave it `None`. |

<!-- api-parameters:KumaClient:end -->

**Preconditions:** construction validates the URL, timeout, and any discovered
key but makes no request. Authenticated read methods require a valid key.

**Postconditions:** a constructed client is reusable. `entitlements()`,
`strategies()`, and `judge_config()` return validated public mappings;
`strategy_group_catalog()` returns a strict typed catalog. None creates a Run.

**Raises and side effects:** construction raises `ConfigurationError` for local
configuration errors. Read methods make one public Backend GET and may raise
`KumaAuthenticationError`, `KumaPermissionError`, or `KumaRateLimitError`.
Credential discovery may read the environment or user credential file; the key
is never included in `repr(client)`, and no method contacts MCP, a model, or a
database directly.

### `strategy_group_catalog`

`strategy_group_catalog()` takes no arguments.

**Returns:** immutable `StrategyGroupCatalog` containing `catalog_release`, the exact `default` declaration, and canonically ordered `groups`. Each `StrategyGroup` exposes `id`, `version`, `display_name`, `description`, `required_capabilities`, `available`, and `limits`; limits contain `max_steps` and `supported_difficulties`.

**Preconditions:** the client has an accepted official credential.

**Postconditions:** the complete public catalog has passed the closed schema, bounds, ordering, uniqueness, and safe-default checks. Callers can use `group(declaration)` for an exact coordinate lookup and `to_dict()` for detached canonical JSON.

**Raises and side effects:** performs one authenticated public catalog read. Authentication, permission, or quota failures preserve their `KumaError` subclasses; malformed or legacy data raises `ValidationError`. It does not create a Case or run local suggestion.

## Strategy Group API

Use the [Strategy Groups guide](strategy-groups.md) for the CLI and Agent Profile workflow.

| Public name | Accepted input / exposed fields | Result and failure behavior |
| --- | --- | --- |
| `StrategyGroupDeclaration` | Exact `id` and `version`; `to_dict()` adds `kuma.strategy_group_selection.v1`. | Immutable Agent Profile-ready coordinate. |
| `StrategyGroup` | `id`, `version`, `display_name`, `description`, `required_capabilities`, `available`, and group `limits`. | Immutable validated catalog entry; `coordinate` returns `(id, version)` and `to_dict()` returns detached JSON. |
| `StrategyGroupCatalog` | `catalog_release`, exact `default`, and ordered `groups`. | `group(declaration)` returns the exact entry or `None`; `to_dict()` returns canonical catalog JSON. |
| `ResolvedStrategyGroup` | Selected `group`, `selection_source`, and `catalog_release`. | `to_declaration()` returns the Agent Profile object; `to_wire()` returns the closed resolved public selection. |
| `validate_strategy_group_declaration(value)` | Plain mapping with exactly `schema_version`, `id`, and `version`. | Returns `StrategyGroupDeclaration`; unknown fields, versions, or invalid text raise `ValidationError(code="strategy_group_invalid")`. |
| `validate_strategy_group_catalog(value)` | Complete closed catalog mapping. | Returns `StrategyGroupCatalog`; malformed fields, ordering, limits, coordinates, or default fail closed. |
| `validate_strategy_group_wire_selection(value)` | Complete resolved mapping with schema version, group ID/version, source, and catalog release. | Returns a detached public mapping; invalid or extra fields fail closed. Intended for advanced Provider boundaries. |

The schema constants `STRATEGY_GROUP_SELECTION_SCHEMA_VERSION` and `STRATEGY_GROUP_CATALOG_SCHEMA_VERSION` expose the two accepted versions. These value objects and validators perform no network, filesystem, Agent, or model operation.

## Agent capability API

Use the [Agent tool capabilities guide](agent-tool-capabilities.md) for the closed JSON schema, CLI workflow, Agent Profile path rules, and privacy boundary.

| Public name | Input | Return value and side effects |
| --- | --- | --- |
| `scan_agent_tools(tools)` | A list or tuple of 1–100 plain tool mappings. | Returns immutable `AgentCapabilities` with `scanner_generated` provenance. Does not inspect framework objects or execute tools. |
| `validate_agent_capabilities(value)` | A complete plain `kuma.agent_tool_capabilities.v1` mapping. | Returns validated, canonically ordered `AgentCapabilities`; invalid, oversized, or sensitive data fails closed. |
| `load_agent_capabilities(path)` | UTF-8 JSON file up to the documented bound. | Reads and validates one file; returns `AgentCapabilities`. |
| `save_agent_capabilities(document, path)` | A mapping or `AgentCapabilities` plus an explicit destination whose parent already exists. | Revalidates and atomically writes canonical JSON; returns the resolved `Path`. |
| `scan_agent_tool_manifest(path)` | Explicit UTF-8 scanner-input JSON manifest. | Reads only that file and returns generated `AgentCapabilities`; no Agent import, repository traversal, tool execution, or network. |

`AgentCapabilities`, `ToolCapability`, and `ResourceScope` are immutable public values with detached `to_dict()` output. `AGENT_CAPABILITIES_SCHEMA_VERSION` identifies the accepted document version. Loading or saving may raise `ValidationError` or `SensitiveDataError`; none of these APIs uploads the document.

An Agent Profile link to a reviewed capability file opts official Case generation
into uploading its complete normalized `tool_capabilities`, including schema
descriptions/defaults/examples. Local helper calls alone do not upload. Omission
sends no declaration; invalid/sensitive content fails before network I/O with
`tool_capabilities_invalid` / `sensitive_data_blocked`, with no sensitive override.
See [Agent tool capabilities](agent-tool-capabilities.md) for bounds and compatibility.

## OpenTelemetry

### External OTLP Trace receiver

`from kuma.otlp import OtlpTraceReceiver` requires the `otlp` optional extra.

<!-- api-parameters:OtlpTraceReceiver:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `capture` | `TraceEvidenceCapture \| None` | `None` | Reuse the capture passed to the Run, or create one with default limits. Available as `receiver.capture`. |
| `port` | `int` | `0` | IPv4 loopback port from 0 through 65535. Zero selects a free port. |
| `max_request_bytes` | `int` | `8388608` | Compressed and decoded request-body ceiling, 1 through 16777216 bytes. Capture limits independently bound retained Evidence. |

<!-- api-parameters:OtlpTraceReceiver:end -->

**Returns:** a receiver context manager. Entry starts a loopback listener; exit
revokes its active lease, closes the socket, and joins its request thread.
Invalid configuration or a missing optional dependency raises `ConfigurationError`.
Socket binding errors propagate as `OSError`. Keep the receiver inside the trusted
local harness and supply `receiver.capture` to `create_run`.

`receiver.step()` returns a context manager yielding a dictionary with
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`, and
`OTEL_EXPORTER_OTLP_TRACES_HEADERS`. It takes no arguments. Call it after
`Run.get_input()` and flush the external exporter before exit, then call `submit`.
Overlapping leases or an inactive Input/receiver raise `ConfigurationError`.
The headers contain an ephemeral bearer for that Input. Exit revokes it and waits
for in-flight ingestion; it has no Agent execution or Run transition side effects.
The receiver accepts up to 1000 spans per batch using HTTP Protobuf, with optional
bounded gzip. Existing Trace body/privacy contracts apply. See the
[complete Java example](../examples/java_agent/README.md).

HTTP header/body reads have a two-second total deadline per accepted connection;
partial traffic does not reset the deadline or indefinitely delay context exit.

Install `kuma-defuzex[otel]` before importing `kuma.otel`.

<!-- api-parameters:configure_trace_evidence:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `tracer_provider` | OTel SDK Provider \| `None` | `None` | Selects the in-process OTel Provider from which KUMA receives ended spans. Pass your application's existing Provider when it is not global; `None` uses the current global Provider. KUMA adds a processor but never replaces or resets the Provider. |
| `logger_provider` | OTel SDK Provider \| `None` | `None` | Selects an existing in-process OTel LoggerProvider for bounded native log metadata. Pass it for explicit OTel log capture; `None` leaves logs unattached in explicit mode. KUMA never replaces it. |
| `limits` | `TraceEvidenceLimits \| None` | `None` | Controls how much Trace data one Run may retain. Pass custom limits for tighter memory/privacy budgets; `None` uses the bounded defaults below. |

<!-- api-parameters:configure_trace_evidence:end -->

**Returns:** a `TraceEvidenceCapture` for
`create_run(trace_evidence=...)`.

**Preconditions:** install the `otel` extra and configure an in-process OTel SDK
Provider that accepts span processors. Pass a non-global Provider explicitly.

**Postconditions:** one KUMA processor is attached and the returned capture can
associate ended spans with a Run. Existing instrumentation and exporters remain
installed.

**Raises and side effects:** invalid Providers or limits raise
`ConfigurationError`. Registration mutates the selected Provider; call once for
an explicitly managed Provider. Only bounded allowlisted data is retained;
recognized model/tool bodies use separate redacted projections. This does not
allow arbitrary source/log bodies, credentials or private Rubrics. See
[OpenInference capture](openinference.md) for the exact body contract.

<!-- api-parameters:TraceEvidenceLimits:start -->

| Argument | Type | Required/default | What happens when the limit is reached |
| --- | --- | --- | --- |
| `max_spans` | positive `int` | `200` | After this many ended spans have been retained for a Run, additional spans are dropped and the Evidence reports the drop instead of growing memory without bound. |
| `max_attributes` | positive `int` | `32` | Keeps at most this many safe, allowlisted attributes on each span; additional attributes are dropped and counted. Sensitive attributes remain rejected regardless of this number. |
| `max_events_per_span` | positive `int` | `20` | Keeps at most this many safe OTel events on each span; later events are dropped and reported. |
| `max_text_length` | positive `int` | `256` characters | Bounds retained metadata strings. Tool arguments/results use a separate 4 MiB canonical JSON limit and are omitted whole, never truncated. See [Runtime Trace](runtime-trace.md). |
| `max_total_bytes` | positive `int` | `8388608` bytes (8 MiB) | Caps the compact JSON size of all committed Trace envelopes in one Run. Run-managed captures reserve an equal share for each remaining Case step so early steps cannot consume the entire budget; unused bytes remain available to later steps. KUMA deterministically drops excess Trace data and reports the loss; the value must still fit the smallest valid envelope. |
| `max_log_records` | positive `int` | `200` | Keeps at most this many normalized OTel log records per step; excess records are dropped and reported. |
| `max_log_bytes` | positive `int` | `128000` bytes | Caps structured OTel log artifacts committed across one Run; raw log bodies are not retained. |

<!-- api-parameters:TraceEvidenceLimits:end -->

**Preconditions:** every value is a positive integer and `max_total_bytes` is
large enough for the required envelope.

**Postconditions:** the immutable limits make capture drop or truncate excess
Trace data with an explicit reason instead of exceeding the configured bounds.
Increasing a limit never broadens the privacy allowlist.

## Public result contracts

The main immutable contracts are exported from `kuma`:

| Type | Important fields and meaning |
| --- | --- |
| `KumaInput` | `run_id`, `case_id`, `input_id`, zero-based `index`, `payload_type`, frozen `payload`, public constraints, schema version, and public extensions. |
| `Submission` | Correlated IDs, terminal step `status`, JSON output/error, capture completeness, bounded logs/file Evidence, dropped/missing counters, schema version, and extensions. |
| `HistoryItem` | One `KumaInput` paired with its ID-matching `Submission`. |
| `TestReport` | `report_id`, `run_id`, `status` (`pass`, `issue`, or `insufficient_evidence`), confidence, stop reason, public issues/evidence gaps, and extensions. Official reports with negotiated assessment include a local `claim_coverage_notice` even on pass; non-pass reports may also include `explanation` and `step_explanations`. These do not change verdict/confidence or fabricate gaps; see [interpretation and limits](judge-assessment.md#local-readable-explanations). |
| `CaptureStatus` | Completeness for file snapshot/diff, logs, sensitive scan, and traces. Each component is `complete`, `partial`, `failed`, or `skipped`. |

Private Rubrics, prompts, model settings, and Core records are not part of these
objects. Runtime Evidence v1 is hash-only; explicitly negotiated v2 may carry a
bounded, scanned completed Agent output. Both formats are defined in the
[Runtime Evidence contract](runtime-evidence.md).

## Capture and trajectory callable contracts

These tables cover the stable local capture and trajectory APIs, including context
lifecycle methods (normally invoked by `with` / `async with`, not manually).
Types and Python default expressions are checked against source signatures.
See [capture lifecycle](capture-lifecycle.md) and [trajectory workspace](trajectory-platform.md)
for complete workflows and serialized field bounds. Local preparation does not
verify that user declarations are true. Hosted operations remain explicit.

### capture

Record normal Agent execution and save locally; never implicitly upload or Judge.

<!-- api-parameters:capture:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `task` | str | Required | Actual original task, nonblank1..12000 characters; recognized secrets are redacted before persistence, with a limitation. The Agent input is caller-owned and is never mutated. No success/reference is invented. |
| `framework` | str | `'existing'` | existing (default) observes application-owned OTel wiring; openai/langchain/langgraph explicitly enable the supported optional hook. |
| `tracer_provider` | Any | `None` | Existing SDK-compatible provider, preserved as-is. None reuses compatible global/established KUMA wiring, or creates a private no-exporter provider for an explicit selected integration. |
| `repo_path` | str \| os.PathLike | `'.'` | Existing Agent workspace, default current directory; no scanning. |
| `storage_path` | str \| os.PathLike \| None | `None` | Existing external parent, disjoint from workspace, or None for ~/.kuma-captures. Default root and .kuma/captures are created on entry. |
| `tools` | list[dict] \| None | `None` | Optional complete v1 tool policies; None means unknown, [] declares no tools. No tool catalog is inferred from observed calls. |
| `requirements` | dict \| None | `None` | Optional complete advanced v1 profile with objective equal to the original task; mutually exclusive with tools. No JSON file required. |
| `limits` | TraceEvidenceLimits \| None | `None` | TraceEvidenceLimits or None for existing observation limits. Omissions and zero capture remain visible; no hidden whole-field truncation. |

<!-- api-parameters:capture:end -->

**Returns:** One-shot CaptureSession, supporting with/async with and stable capture_id.

**Raises:** ValidationError: Invalid task/rules. ConfigurationError: Unsupported or unavailable explicit integration, unsafe storage or normal-exit save failure.

**Side Effects:** Explicit optional process instrumentation and local redacted persistence on exit. No key, model request, network, Case, Judge or credit consumption. User exporters retain their independent behavior. Agent exceptions/cancellation propagate; inspect persistence_status/error if saving also fails.

### export_capture

Load one durable ID and atomically export its task-bound document without overwrite.

<!-- api-parameters:export_capture:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `identifier` | str | Required | Durable cap_ plus 32 lowercase hex ID from a saved session. |
| `path` | str \| os.PathLike | Required | New destination path with an existing parent; existing targets are rejected. |
| `repo_path` | str \| os.PathLike | `'.'` | Existing original workspace path, default current directory. |
| `storage_path` | str \| os.PathLike \| None | `None` | Original disjoint external parent or None for ~/.kuma-captures. |

<!-- api-parameters:export_capture:end -->

**Returns:** Absolute pathlib.Path of the atomically published task-bound JSON document.

**Raises:** KumaError: Invalid/missing capture, unsafe paths, malformed content or local I/O failure; messages do not expose raw OS details.

**Side Effects:** Bounded local read and atomic no-overwrite write; pinned handles are released. Partial/failed capture status is preserved. No credentials, network or billing.

### judge_capture

Explicitly review a saved execution using one Kuma credential and the bound task.

<!-- api-parameters:judge_capture:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `identifier` | str | Required | Durable cap_ ID in the selected local store. |
| `repo_path` | str \| os.PathLike | `'.'` | Original workspace, default current directory. |
| `storage_path` | str \| os.PathLike \| None | `None` | Original external storage parent, or None for the default. |
| `client` | KumaClient \| None | `None` | KumaClient or None to resolve KUMA_API_KEY/normal credentials. No provider/storage credential is needed; Agent runtime keys remain separate. |
| `requirements` | dict \| None | `None` | Optional richer v1 rules retaining the exact original task. |
| `idempotency_key` | str \| None | `None` | Optional8..200 ASCII protocol key. None hashes immutable capture/submission content. Same input/key recovers the same review; selecting a fresh key deliberately requests a separate paid review. |

<!-- api-parameters:judge_capture:end -->

**Returns:** Validated completed/stopped public review, also durably retained locally.

**Raises:** KumaError: Invalid/unavailable local capture, empty evidence, auth/scopes, credits, unknown timeout outcome, remote failure or local save failure.

**Side Effects:** One explicit upload then one Judge request, no retry or Agent rerun. Existing configured fixed Judge fee applies only to completed validated review. A local result-save failure does not undo a completed server review: recover with identical inputs/key. No original trace is rewritten or deleted.

### list_captures

Read at most limit summaries, newest local file first; never emit task/body content.

<!-- api-parameters:list_captures:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `repo_path` | str \| os.PathLike | `'.'` | Existing original workspace path, default current directory; no scanning. |
| `storage_path` | str \| os.PathLike \| None | `None` | Original external parent or None for ~/.kuma-captures; must exist. |
| `limit` | int | `20` | Integer 1..100, default 20; maximum summaries, not automatic pagination. |

<!-- api-parameters:list_captures:end -->

**Returns:** List of metadata dicts sorted by file modification time then name, newest first. Each has capture_id, execution_status, capture_status, span_count, operation_counts and reasons. Counts describe retained observations, not inferred Agent activity.

**Raises:** ValidationError: Invalid limit or invalid stored capture content. ConfigurationError: Absent/unreadable/unsafe store or file; no fake complete entry.

**Side Effects:** Bounded no-follow local file reads with handles closed on completion/failure. Does not create a store, read credentials, contact a service or output task bodies.

### CaptureSession

Construct a one-shot local capture; prefer the capture() factory.

<!-- api-parameters:CaptureSession:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `task` | str | Required | Original nonblank task, 1..12000 characters; recognized secrets are redacted. |
| `framework` | str | `'existing'` | existing, openai, langchain or langgraph; default existing preserves wiring. |
| `tracer_provider` | Any | `None` | Application-owned provider or None for the selected integration. |
| `repo_path` | str \| os.PathLike | `'.'` | Existing workspace path, default current directory; never scanned. |
| `storage_path` | str \| os.PathLike \| None | `None` | Existing disjoint external parent, or None for ~/.kuma-captures. |
| `tools` | list[dict] \| None | `None` | Complete declared tool policies, None for unknown or [] for no tools. |
| `requirements` | dict \| None | `None` | Advanced v1 rules matching the task; mutually exclusive with tools. |
| `limits` | TraceEvidenceLimits \| None | `None` | TraceEvidenceLimits or None for observation defaults; at least 2048 bytes. |

<!-- api-parameters:CaptureSession:end -->

**Returns:** Inactive CaptureSession with stable capture_id and persistence_status=not_saved.

**Raises:** ValidationError: Invalid task or declared rules, without running the Agent. ConfigurationError: Unsupported framework or invalid limits.

**Side Effects:** Allocates local state only. Context entry acquires storage/instrumentation; exit finalizes and saves. No credentials, network, upload or Judge request.

### CaptureSession.capture_id

Return the stable opaque local capture identifier.

<!-- api-parameters:CaptureSession.capture_id:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |


<!-- api-parameters:CaptureSession.capture_id:end -->

No caller parameters.

**Returns:** str of cap_ plus 32 lowercase hexadecimal characters; available before entry. A durable CLI lookup works only after successful persistence.

**Side Effects:** None; does not read storage or prove the capture has been saved.

### CaptureSession.__enter__

Enter this one-shot observation after local storage preflight.

<!-- api-parameters:CaptureSession.__enter__:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |


<!-- api-parameters:CaptureSession.__enter__:end -->

No caller parameters.

**Returns:** This CaptureSession, active for context-propagated instrumented Agent calls.

**Raises:** ConfigurationError: Re-entry, unsafe/unwritable storage, unavailable integration or observation setup failure. A failed entry cannot be retried on this session.

**Side Effects:** Creates capture directories as needed, probes writes and attaches explicit instrumentation before observation. No Agent execution or KUMA network request. The caller must exit the context; application-owned providers are not shut down.

### CaptureSession.__aenter__

Enter the same capture lifecycle from async with.

<!-- api-parameters:CaptureSession.__aenter__:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |


<!-- api-parameters:CaptureSession.__aenter__:end -->

No caller parameters.

**Returns:** This active CaptureSession, with the same one-shot semantics as __enter__.

**Raises:** ConfigurationError: The same storage/instrumentation/entry failures as __enter__.

**Side Effects:** Performs synchronous local setup on the calling thread; adds no background task. Application-owned providers remain owned by the caller.

### CaptureSession.__exit__

Finalize observation and persist redacted facts without hiding an Agent failure.

<!-- api-parameters:CaptureSession.__exit__:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `exc_type` | Any | Required | Exception class supplied by with, or None for normal completion. |
| `exc` | Any | Required | Original exception instance or None; not copied into public error messages. |
| `traceback` | Any | Required | Original traceback or None, forwarded to the observation context. |

<!-- api-parameters:CaptureSession.__exit__:end -->

**Returns:** False in every normal return, so an original Agent exception is not suppressed.

**Raises:** ConfigurationError: Finalization/save failed on normal exit; inspect persistence_status/error and export() for recoverable finalized data.

**Side Effects:** Closes the observation and attempts one local save; marks persistence saved or failed. If an Agent exception/cancellation already exists, preserves it instead of raising the save error. Repeated exit is a no-op; no upload occurs.

### CaptureSession.__aexit__

Finalize on async exit while retaining the original cancellation identity.

<!-- api-parameters:CaptureSession.__aexit__:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `exc_type` | Any | Required | Exception class from async with, or None on normal completion. |
| `exc` | Any | Required | Original exception/cancellation instance or None; never rewritten. |
| `traceback` | Any | Required | Original traceback or None passed to observation finalization. |

<!-- api-parameters:CaptureSession.__aexit__:end -->

**Returns:** False, preserving exception propagation as in __exit__.

**Raises:** ConfigurationError: Local save/finalization failure on otherwise normal exit.

**Side Effects:** Synchronously finalizes and saves on the calling thread, without scheduling background work. On cancellation, save failure is exposed through persistence status/error rather than replacing that cancellation. No upload or Judge call.

### CaptureSession.export

Return detached finalized capture data, including task, gaps and actual execution status.

<!-- api-parameters:CaptureSession.export:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |


<!-- api-parameters:CaptureSession.export:end -->

No caller parameters.

**Returns:** Detached kuma.capture.v1 dict containing the task, observation, instrumentation and capture_id; may be available even when the local save failed.

**Raises:** ConfigurationError: Finalization has not produced a document; no synthetic successful capture is returned.

**Side Effects:** None; reads only finalized in-memory state, with no I/O or upload.

### KumaClient.trajectory

Return the optional trajectory client using this client's existing KUMA credential.

<!-- api-parameters:KumaClient.trajectory:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `timeout` | float | `180` | Positive finite per-request seconds, default 180 for bounded process review. This does not change this KumaClient's timeout or enable automatic retries. |

<!-- api-parameters:KumaClient.trajectory:end -->

**Returns:** kuma.trajectory_client.TrajectoryClient for explicit import, requirements, search, batch review, labels and comparison; no network request occurs on construction.

**Raises:** ConfigurationError: Invalid timeout, before any I/O.

**Side Effects:** None; no extra key, model/tracing dependency, upload or credit consumption.

### TrajectoryClient

Reuse existing KUMA configuration without reading a new key or changing parent timeouts.

<!-- api-parameters:TrajectoryClient:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `client` | Any | Required | Existing kuma.KumaClient, normally produced by client.trajectory(). |
| `timeout` | float | `180` | Positive finite per-request seconds, default 180 for bounded process reviews. |

<!-- api-parameters:TrajectoryClient:end -->

**Raises:** ConfigurationError: Invalid timeout, before any HTTP request.

**Returns:** TrajectoryClient sharing the existing credential through a copied transport.

**Side Effects:** None; a shallow private transport copy disables automatic retries for this capability. No model client, tracing platform, credentials file, database or collector is added.

### TrajectoryClient.request

Execute one allowlisted workspace command using normal KUMA authentication.

<!-- api-parameters:TrajectoryClient.request:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `action` | str | Required | Exact supported workspace action; see docs/trajectory-platform.md. |
| `data` | Mapping[str, Any] | Required | Action-specific finite JSON fields; no owner, provider URL or credentials. |
| `idempotency_key` | str \| None | `None` | Required for reviews.run, 8..200 ASCII letters/digits/._:-. Reuse unchanged to recover the same logical paid review; never reuse with new input. |

<!-- api-parameters:TrajectoryClient.request:end -->

**Returns:** Detached user-owned result from kuma.trajectory_workspace.v1, with no automatic pagination.

**Raises:** ValidationError: Unsupported action, invalid key or unsafe input before HTTP. ProviderError: Malformed/private/sensitive remote response. KumaError: Auth/scope, credits, conflicts, feature, timeout or platform failure.

**Side Effects:** One authenticated public Backend POST, no retries. reviews.run may consume the existing Judge fee only after completion; imports, storage, labels and reads never invoke Judge. A timeout is an unknown server outcome; retry explicitly with the same key, not a new key.

### TrajectoryClient.save_requirements

Explicitly save one caller requirements revision.

<!-- api-parameters:TrajectoryClient.save_requirements:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `requirements` | Mapping[str, Any] | Required | Complete v1 requirements mapping; validated locally before transport. |

<!-- api-parameters:TrajectoryClient.save_requirements:end -->

**Returns:** Owned immutable requirements receipt with ID, hash and display summary.

**Raises:** ValidationError: Invalid or sensitive profile before HTTP; request errors also apply.

**Side Effects:** One authenticated write, without model calls or Judge charges. Content is immutable.

### TrajectoryClient.export

Retrieve an exact canonical JSON artifact without changing large integers or source types.

<!-- api-parameters:TrajectoryClient.export:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `kind` | str | Required | requirements, traces, reviews, datasets, experiments or feedback. |
| `identity` | str | Required | Owned document ID returned by the workspace. |

<!-- api-parameters:TrajectoryClient.export:end -->

**Returns:** UTF-8 JSON bytes bound to the requested ID digest, at most 6 MiB uncompressed. Response identity, category, declared digest and decoded-byte hash must agree. The caller owns saving the bytes.

**Raises:** ValidationError: Unsupported category or malformed/category-mismatched ID; rejected before HTTP. Standard request errors also apply. ProviderError: Bad encoding, expansion limit or mismatched content hash.

**Side Effects:** One read-only POST; no local file, model, credit or automatic retry.

### TrajectoryClient.upload

Explicitly import a prepared submission with declared version/case/tags; never evaluates it.

<!-- api-parameters:TrajectoryClient.upload:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `submission` | Mapping[str, Any] | Required | v1 prepare_submission/prepare_observation output, revalidated before HTTP. |
| `agent_version` | str \| None | `None` | Optional caller build/model label up to 80 characters, not verified identity. |
| `case_id` | str \| None | `None` | Optional stable caller test-case ID up to 128 characters; required for version matching. |
| `tags` | list[str] \| None | `None` | Optional list of up to 32 labels of 1..120 characters, default empty. |

<!-- api-parameters:TrajectoryClient.upload:end -->

**Returns:** Owned trace document receipt; unchanged content reuses its content identity.

**Raises:** ValidationError: Invalid/mismatched input before HTTP; standard request errors also apply.

**Side Effects:** One explicit Backend write; no model, credits or file changes.

### TrajectoryClient.upload_atif

Upload existing ATIF without collecting or executing an Agent.

<!-- api-parameters:TrajectoryClient.upload_atif:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `document` | Mapping[str, Any] | Required | ATIF-v1.8 source JSON; original values and coordinates remain unchanged. |
| `requirements` | Mapping[str, Any] | Required | Actual reviewed v1 requirements; unknown tools stay null. |
| `agent_version` | str \| None | `None` | Optional caller build label, at most 80 characters. |
| `case_id` | str \| None | `None` | Optional stable task identity, at most 128 characters. |
| `tags` | list[str] \| None | `None` | Optional list of at most 32 metadata labels, each 1..120 characters. |

<!-- api-parameters:TrajectoryClient.upload_atif:end -->

**Returns:** Owned trace receipt from upload(); no model review is automatically requested.

**Raises:** ValidationError: Unsafe/invalid/oversized source or declarations before HTTP. KumaError: Existing authentication, scope, transport or storage errors.

**Side Effects:** One explicit Backend write using the existing Kuma key. No file/URL resolution, model calls or Judge credit consumption; the caller's source object is unchanged.

### TrajectoryClient.evaluate

Run the private process Judge on one owned stored trace, without running the Agent.

<!-- api-parameters:TrajectoryClient.evaluate:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `trace_id` | str | Required | Owned document ID returned by upload/list, not a model provider trace URL. |
| `idempotency_key` | str | Required | Required unchanged logical review identity; see request() for constraints. |
| `limits` | Mapping[str, Any] \| None | `None` | Optional integer budgets: decisions 1..12 (default 6), tool_calls 1..24 (12), evidence_characters 1000..120000 (40000), seconds 1..900 (120), source_characters 1000..4194304 (4194304), chunk_characters 1000..350000 (240000). None leaves defaults to the service; deployment lease/timeout constraints may reduce accepted seconds. See docs/trajectory-platform.md. |

<!-- api-parameters:TrajectoryClient.evaluate:end -->

**Returns:** Completed report with blank human labels, or an explicit stopped artifact with reason.

**Raises:** ValidationError: Invalid trace ID, key, budget or sensitive input before HTTP. ProviderError: Malformed or unsafe remote response. KumaError: Missing authentication, scope, credit, timeout or service failure; a timeout is an unknown outcome, not evidence that no review occurred.

**Side Effects:** One explicit billable request; completed review uses the configured Judge fixed fee. No hidden retries, gold-label generation or automatic score penalties for retries/gaps.

### TrajectoryClient.run_experiment

Review each frozen dataset trace sequentially and preserve failed/missing cases.

<!-- api-parameters:TrajectoryClient.run_experiment:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `name` | str | Required | Nonblank experiment label up to 200 characters, frozen by the server. |
| `dataset_id` | str | Required | Owned dataset document with 1..50 ordered trace IDs. |
| `idempotency_key` | str | Required | Required run identity; deterministic child keys recover completed trace reviews on explicit rerun. Changing name, dataset or limits changes children. |
| `limits` | Mapping[str, Any] \| None | `None` | Optional per-trace budgets, forwarded unchanged to evaluate. |

<!-- api-parameters:TrajectoryClient.run_experiment:end -->

**Returns:** Always a dict with exactly experiment, reviews and errors on normal return. experiment is a saved experiment receipt (dict), or None only when no review artifact was returned. reviews is an ordered list of validated completed/stopped review dicts. errors is an ordered list of {trace_id: str, code: str} for individual evaluate() KumaError failures; it contains no exception messages. If all evaluations raise KumaError, reviews is empty and experiment is None; the outer result is never None. A stopped artifact is retained in reviews, not errors, and does not establish successful Agent behavior or complete review.

**Raises:** ValidationError: Invalid name/key before the dataset read, or unsafe child-key seed during iteration; these errors are not converted to per-trace errors. ProviderError: Invalid dataset response before evaluations begin. KumaError: Initial dataset read or final experiment save fails, with no returned wrapper. Only KumaError from individual evaluate() calls is accumulated. BaseException: Cancellation/KeyboardInterrupt propagates immediately; completed remote work is not rolled back. Unexpected non-KumaError also propagates.

**Preconditions:** Use an owned frozen dataset and a credential authorized for its traces. Explicit recovery must preserve name, dataset, limits and the run key.

**Postconditions:** Normal return preserves each returned artifact or per-trace error in input order. A raised read/save/cancellation error does not roll back remote work.

**Security/Privacy:** Uses the existing authenticated trajectory boundary; errors contain safe categories, not exception text. Returned reviews remain caller-owned data.

**Side Effects:** One dataset read, up to 50 explicit review requests and, only if reviews exist, one experiment write. No experiment is saved when every evaluation raises. Each newly completed review can charge its configured Judge fee; storage is separate. Does not run evaluated Agents, synthesize missing traces or calculate an overall ranking.

### prepare_review

Detach and redact existing task/tool/event records for local Core review.

<!-- api-parameters:prepare_review:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `task` | str | Required | Required original task text, 1..12000 characters after trimming. Preserve amendments in the text or recorded events; do not add a rubric. |
| `events` | Sequence[Mapping[str, Any]] | Required | Ordered exported event objects, 1..10000 entries. Each requires a nonempty string ``event`` field. Preserve observed fields, including tool, arguments, result, ok, turn_id and operation IDs when available. This helper neither collects telemetry nor parses vendor log files. |
| `sample` | str | `'trajectory'` | Nonempty review label, at most 200 characters; default trajectory. |
| `tools` | Sequence[Mapping[str, Any]] \| None | `None` | Actual tools available during execution, at most 256 objects with distinct nonempty ``name`` fields. Include descriptions, parameter schemas, prerequisites and availability changes when known. None means not supplied; an empty list explicitly declares no available tools. |
| `limitations` | Sequence[str] | `()` | At most 32 known capture gaps as nonempty strings. Missing outputs, changed tool versions and partial logs belong here. |

<!-- api-parameters:prepare_review:end -->

**Returns:** Detached ``kuma.trajectory_review_bundle.v1`` JSON. ``content_status`` distinguishes present from redacted content; the original is unchanged.

**Raises:** ValidationError: Invalid shape, graph, size or remaining sensitive content. Diagnostics never include source values. Nothing is silently truncated.

**Side Effects:** None. No filesystem, environment, API key, model or network access.

**Security/Privacy:** Reuses KUMA's existing finite-JSON and Evidence redaction/scanning boundary, capped at 4 MiB and depth 32. There is no allow-sensitive override. Detection covers recognized patterns, not all possible private information. Review prepared contents before sharing them outside your environment.

### encode_review

Revalidate and serialize a prepared bundle as canonical UTF-8 JSON bytes.

<!-- api-parameters:encode_review:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `bundle` | Mapping[str, Any] | Required | A prepared v1 bundle. Content is checked and sanitized again so edits made after preparation cannot bypass the Evidence boundary. |

<!-- api-parameters:encode_review:end -->

**Returns:** Sorted, compact UTF-8 JSON suitable for a caller-owned local file. Redacted status is retained when revalidation makes no further changes.

**Raises:** ValidationError: Unsupported schema, fields or content. No raw value is echoed.

**Side Effects:** None; the caller owns file creation and any later submission authorization. This is a local preparation API, not a live hosted review endpoint.

### classify_observation

Group recorded operations structurally, without turning errors into Agent fault.

<!-- api-parameters:classify_observation:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `observation` | Mapping[str, Any] | Required | Completed kuma.observe().export() data, validated using its existing contract. |

<!-- api-parameters:classify_observation:end -->

**Returns:** v1 inventory of exact trace/span/parent IDs, operation classes, recorded statuses and durations; unmapped operations are other. Counts describe retained spans only. Includes capture status/gaps; no semantic task category, retry blame, success or cost is inferred.

**Raises:** ValidationError: Invalid, sensitive or oversized observation.

**Side Effects:** None; no collector, Agent, tool, network or filesystem operation.

### prepare_observation

Prepare actual observed spans with caller requirements and explicit capture gaps.

<!-- api-parameters:prepare_observation:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `observation` | Mapping[str, Any] | Required | Completed local export from existing kuma.observe; never an active session. |
| `requirements` | Mapping[str, Any] | Required | Reviewed v1 task/tool/retry/budget document; full field guide in platform docs. |
| `sample` | str | `'trajectory'` | Review label, nonblank up to 200 characters, defaults to trajectory. |

<!-- api-parameters:prepare_observation:end -->

**Returns:** kuma.trajectory_submission.v1 with detached bundle, requirements, content revision, recorded observation ID and structural inventory. Empty capture is represented by a genuine capture-summary event, never fabricated Agent activity. No turn IDs are inferred.

**Raises:** ValidationError: Invalid input, unsafe policies or complete review bundle above 4 MiB.

**Side Effects:** None; importing/preparing does not upload, evaluate, save or charge.

### prepare_submission

Bind a native-event bundle to a specific requirements revision for explicit submission.

<!-- api-parameters:prepare_submission:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `bundle` | Mapping[str, Any] | Required | Existing v1 bundle from prepare_review, revalidated with its privacy boundary. |
| `requirements` | Mapping[str, Any] | Required | Reviewed v1 document; objective and tool catalog must match the bundle. |
| `observation_id` | str \| None | `None` | Optional existing obs_ ID for correlation; null for native-file imports. |
| `inventory` | Mapping[str, Any] \| None | `None` | Optional classify_observation output, null for native events; descriptive only. |

<!-- api-parameters:prepare_submission:end -->

**Returns:** Detached v1 submission, with a content fingerprint for immutable evaluation provenance.

**Raises:** ValidationError: Invalid/mismatched bundle, policy or observation ID; no network occurs.

**Side Effects:** None; owner/project identity and server credentials are never caller-controlled fields.

### atif_events

Partition a finite ATIF 1.8 document into lossless, source-addressed records.

<!-- api-parameters:atif_events:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `document` | Mapping[str, Any] | Required | Caller-owned JSON trajectory with unique trajectory IDs, ordered steps and optional nested subagent_trajectories. Unknown fields and artifact references stay data. |

<!-- api-parameters:atif_events:end -->

**Returns:** Detached event list preserving every source field. JSON pointers address original nodes; event order is source traversal, not invented global execution chronology. User-step boundaries supply explicitly derived turn IDs within each execution only.

**Raises:** ValidationError: Invalid, oversized, nonfinite/deep, sensitive or ambiguous source.

**Side Effects:** None. No file/URL resolution, imports of Agent code, network or model calls.

### prepare_atif

Prepare ATIF as a normal public review bundle with exact source coordinates.

<!-- api-parameters:prepare_atif:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `task` | str | Required | Actual caller-provided task/review scope, 1..12000 characters; never inferred. |
| `document` | Mapping[str, Any] | Required | ATIF-v1.8 source object, bounded by existing four-MiB/depth-32 preparation. |
| `sample` | str | `'trajectory'` | Nonblank review label up to 200 characters, default trajectory. |
| `tools` | list[dict[str, Any]] \| None | `None` | Actual declared tools, None for unknown, empty list for explicitly no tools. |
| `limitations` | list[str] \| tuple[str, ...] | `()` | Up to 32 caller-known capture limits. Format notes use remaining slots; Core independently preserves source-format and coordinate semantics. |

<!-- api-parameters:prepare_atif:end -->

**Returns:** Existing v1 bundle usable with prepare_submission/upload, retaining redaction status. Source hash covers the sanitized original document, not unavailable raw private bytes.

**Raises:** ValidationError: Unsafe/invalid source, policy or existing bundle size limits exceeded.

**Side Effects:** None; references and scripts are never opened, fetched or executed. Caller owns submission.

### requirements_template

Return an editable requirements document with explicit unknowns, never invented answers.

<!-- api-parameters:requirements_template:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |


<!-- api-parameters:requirements_template:end -->

No caller parameters.

**Returns:** Detached v1 dictionary. Fill name, version, objective and criteria before validation; null tools means unavailable catalog, whereas an empty list declares no tools.

**Side Effects:** None; does not infer requirements, inspect an Agent or contact a service.

### validate_requirements

Validate and detach a reviewed requirements document without changing caller policies.

<!-- api-parameters:validate_requirements:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `value` | Mapping[str, Any] | Required | Exact v1 template shape. Required nonblank name/version/objective and 1..16 success criteria; task_type may be empty (unclassified); unknown tools/reference and each unknown budget are null. See docs/trajectory-platform.md for every field. |

<!-- api-parameters:validate_requirements:end -->

**Returns:** Detached finite JSON, at most 256 KiB and depth 32, preserving user-declared policies.

**Raises:** ValidationError: Invalid/oversized shape or sensitive content requiring redaction. Requirements with redacted policies are rejected for user correction, not changed.

**Side Effects:** None; no model, network, files, credentials, tool/schema execution or scoring.

### requirements_fingerprint

Return SHA-256 of canonical validated requirements; edits produce a different revision.

<!-- api-parameters:requirements_fingerprint:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `value` | Mapping[str, Any] | Required | Reviewed requirements accepted by validate_requirements. |

<!-- api-parameters:requirements_fingerprint:end -->

**Returns:** Lowercase 64-character content identity, not proof of truth or authority.

**Raises:** ValidationError: The same invalid/sensitive/size conditions as validation.

**Side Effects:** None; does not register or persist a revision.

## Run-managed Trace step budgets

`TraceEvidenceCapture.begin_step` is the lower-level in-process lifecycle method
used by Run. Normal applications let Run supply the input index and Case length.

<!-- api-parameters:TraceEvidenceCapture.begin_step:start -->

| Argument | Type | Required/default | What it does and when to use it |
| --- | --- | --- | --- |
| `run_id` | str | Required | Nonempty owning Run identifier used to bind observed spans. |
| `case_id` | str | Required | Nonempty owning Case identifier used for the shared byte budget. |
| `input_id` | str | Required | Nonempty identifier of the current delivered input/step. |
| `step_index` | int \| None | `None` | Zero-based index, excluding bool; must be at least zero and less than total_steps when both are supplied. None preserves first-come budgeting. |
| `total_steps` | int \| None | `None` | Positive normalized Case input count, excluding bool. With step_index, reserves a share of remaining bytes for each remaining step; not a promised span count. If either is None, historical first-come behavior applies. |

<!-- api-parameters:TraceEvidenceCapture.begin_step:end -->

Returns None and installs one empty active bucket. A previous step must already be
committed, aborted, cancelled or finished. Invalid identifiers/metadata or an
oversized minimal envelope raise ConfigurationError; overlapping active steps
raise RuntimeError and discard the conflicting bucket to avoid cross-step leakage.
Only in-memory state changes: no file reads, Agent execution or network. Privacy
filters and truthful loss reasons remain unchanged. Unused bytes remain available
to later steps; tight budgets can still cause explicit loss.

## Error fields

`support_code: str | None` is an optional opaque support category from exactly
one validated `X-KUMA-Support-Code` response header (`SB000`–`SB003`). Official
busy errors display it alongside a valid response `request_id`; missing/invalid
headers and non-busy errors leave it `None`. It is not inferred from error text,
used for retry decisions or added to the JSON wire. See [error display](public-error-diagnostics.md).

Catch `KumaError` for normal SDK failures. `str(exc)` is a safe user-facing
message. Program logic should use `exc.code`, `exc.retryable`, and
`exc.request_id`; `exc.details` is a bounded public mapping and should be logged
only through an application-approved allowlist.

`request_id: str | None` carries an actual `X-Request-ID` response header only
when it matches 32 lowercase hexadecimal characters. Missing, malformed, or
duplicate headers yield `None`; the SDK never generates a replacement or reads
IDs from the JSON body. For async terminal failures it identifies that poll
response, not the operation/start request. The separate `kreq_…`
`client_request_id` remains the local recovery identity. A valid header can be
server-echoed rather than server-generated; it is not an authentication token.

The same ID is retained for decode/size/status failures and rejected operation
start/poll/result schemas, using only the response being validated. No-response
network failures and local persistence errors are not assigned a prior ID.

Remote error details use exact per-code schemas (HTTP failures and async failed
operations share validation):

| `exc.code` | Allowed `exc.details` | Validation |
| --- | --- | --- |
| `case_step_limit_exceeded` | `{"max_allowed_steps": 10}` | Required non-boolean integer, 1–10. |
| `unsupported_difficulty` | `{"supported_difficulties": ["D0", "D2"]}` | 1–5 unique values from D0–D4; server order is preserved, not required to be sorted. |
| `strategy_capability_mismatch` | `{"missing_capabilities": ["file_change", "tool_call"]}` | 1–7 unique Evidence capabilities in canonical order: file_change, tool_call, command_result, test_result, state_transition, artifact_snapshot, agent_response_claim. |

For example, on `unsupported_difficulty`, show the validated
`exc.details.get("supported_difficulties", [])` so the caller can choose a
supported value. Historical omission of difficulty/capability details yields
`{}`. Present malformed, empty, unknown-key or out-of-range details for these
codes raise `ProviderError(code="invalid_response")`; an invalid async response
does not clear pending recovery state. The Case-limit contract is unchanged.

`invalid_request` additionally accepts optional `details.fields`: 1–16 closed
records with required `field` and `reason`. Fields are the static public serializer
paths listed in [public error diagnostics](public-error-diagnostics.md), not
submitted values. Reasons are required, invalid_type, blank, min_value, max_value,
max_length, invalid_choice, or invalid. Optional expected_type, minimum, maximum,
and allowed_values must match the frozen per-field constraints. The SDK includes
these safe constraints in its English correction message.

For official Judge only, internal `model_invalid_result` / `model_invalid_response`
failures (including older servers) are presented as `ServiceBusyError` with code
`service_busy`, message `Service is busy. Please try again later.`, and no details. The original `retryable`
and response-header `request_id` remain available. No automatic retry, new task,
or fabricated report is created; committed Run history is retained. Input,
authentication, permission and quota errors are not converted. Case generation
keeps the following existing policy.

`model_invalid_result` (and historical `model_invalid_response`) accepts only
optional `{"reason": "invalid_structure"}` with reason from invalid_structure,
invalid_type, out_of_bounds, or invalid_format. Its message explicitly describes
a **service-generated result failure**, not a request to fix user input. Missing
details stay empty: historical failures do not acquire guessed explanations.
Only these two model codes and invalid_request also accept the legacy exact empty
object `details: {}` as absence; null/lists/unknown nonempty details remain invalid.

Other HTTP details remain discarded; unknown async detail shapes remain rejected.
Remote free-form messages, private paths and raw model responses are never shown.
Details do not change retryability, pending identity, billing or automatic retry
policy. See the [unreleased error change notes](public-error-diagnostics.md).
