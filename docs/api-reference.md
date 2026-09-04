# KUMA Python API reference

[简体中文](api-reference.zh-CN.md) | English

This page documents the stable user-facing Python entry points. Types, defaults,
ranges, side effects, and failure behavior match the current implementation.
KUMA uses keyword-only arguments for its main APIs so call sites remain readable.

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

**Raises:** `ConfigurationError` for an invalid key or unresolved credential
location, and `OSError` for a real filesystem failure.

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
| `agent_profile_path` | `str \| os.PathLike[str] \| None` | `None` | Points to the UTF-8 file that describes the Agent, its production scenario, expected behavior, and prohibited boundaries. Supply it for official Case generation. The selected Strategy Group still controls the testing capability/domain/method; profile prose cannot select or override that group. Front matter may contain a closed `strategy_group` coordinate and a relative `tool_capabilities` file; both are validated before Provider I/O. Omit the path only when your custom Case Provider does not need an Agent Profile. |
| `case_provider` | `CaseProvider \| callable \| None` | `None` | Chooses who creates the test Inputs. Leave `None` to request an official Case from KUMA; pass a callable when your application supplies its own local Case. |
| `judge_provider` | `JudgeProvider \| callable \| None` | `None` | Chooses who evaluates all submitted results and builds the final report. Leave `None` for the official Judge, or pass a callable for your own local evaluation. Ignored when `judge=False`. |
| `strategy` | `str` | `"auto"` | Preserves compatibility with services that use an unversioned strategy ID. For current Strategy Groups, put the exact `id` and `version` in Agent Profile front matter. Combining a structured declaration with a non-default legacy value fails instead of creating ambiguous intent. |
| `max_steps` | `int \| None` | `None` | Limits how many test steps this Run may contain. For example, `3` allows one, two, or three steps—it does not force exactly three. `None` uses the official service limit; custom Case Providers require an explicit positive value. An explicit official value above the advertised limit fails before Case generation, and KUMA never truncates a returned Case. |
| `judge` | `bool` | `True` | Controls whether KUMA evaluates the Run after the last Input. Keep `True` to receive a `TestReport`; use `False` when you only want to execute and record the Case, in which case `run.report` remains `None`. |
| `on_failure` | `str` | `"continue"` | Decides what happens after you submit a step as `failed`, `timeout`, or `aborted`. `"continue"` delivers the next Input; `"stop"` ends the Run immediately. |
| `allow_local` | `bool` | `False` | Allows the Run to start outside Docker for trusted local development. It only bypasses the Docker safety prerequisite: it does not sandbox the Agent, expand file access, or weaken validation and privacy checks. |
| `track_files` | `bool` | `True` | Tells KUMA to compare repository file metadata before and after each Input so the Judge can see which files were created, modified, deleted, or renamed. Set `False` when file changes are irrelevant or unavailable. |
| `upload_diff` | `bool` | `False` | Adds bounded changed text to file Evidence instead of sending only paths, hashes, sizes, and change types. Enable only when the Judge needs the actual diff and the repository text is safe to disclose; requires `track_files=True`. |
| `save_local` | `bool` | `False` | Writes a local JSON copy of each committed Submission under `.kuma/runs/<run_id>/`. Use it for debugging or audit records. It does not replace submission to an official Judge. |
| `allow_sensitive` | `bool` | `False` | Lets ordinary Evidence continue when KUMA's scanner flags content as potentially sensitive. Leave `False` unless you reviewed that content and intend to disclose it; this never allows secrets into OTel Trace Evidence. |
| `timeout` | `float` | `300.0` seconds | Limits one HTTP connection attempt to the public KUMA service. Lower it to fail individual network calls sooner. It does not limit the total time spent waiting for Case generation or Judge completion. |
| `operation_wait_timeout` | `float` | `600.0` seconds | Limits the total synchronous wait for one official Case or Judge operation, including polling. If it expires, KUMA raises a retryable timeout and keeps safe recovery metadata so the same operation can be resumed. |
| `max_retries` | `int` | `2` | Sets how many additional attempts KUMA may make after a transient HTTP failure; accepted values are 0–5. Retries reuse the same idempotency key and do not intentionally create another Case or Judge operation. |
| `api_key` | `str \| None` | `None` | Supplies the official-service credential for this Run only. Use it to override the environment or saved credential. With `None`, KUMA checks `KUMA_API_KEY` and then the user credential file. Fully local Provider combinations need no key. |
| `trace_evidence` | `TraceEvidenceCapture \| None` | `None` | Supplies a specific in-process OTel capture and its limits for this Run. Pass the object returned by `configure_trace_evidence()` when you need explicit control. With `None`, KUMA safely reuses a compatible global Provider when available; otherwise the Run continues without Trace Evidence and records a warning. |
| `scan_strategy_group` | `bool` | `False` | Explicitly enables conservative local Strategy Group suggestion for an official Case. KUMA compares only closed declared and intrinsic Runtime Evidence capabilities; it never executes tools or guesses from names, descriptions, schemas, resources, access, or side effects. A unique reliable match is selected; ties and no-match results use the catalog's exact default. An explicit Agent Profile selection always has priority. |

<!-- api-parameters:create_run:end -->

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

### Custom Case boundary

A custom Case supplies only public Inputs and constraints. Its `rubric`
compatibility slot must be `None`; mappings containing `rubric`,
`private_rubric`, or `rubric_context` fail with
`custom_rubric_not_supported` before upload. When the official Judge evaluates
a custom Case, KUMA sends that closed public Case directly and does not create
or transmit caller-authored criteria, a Rubric ID, or a private revision ID.

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

## JSON serialization

`kuma.to_json(value)` converts an exact public immutable KUMA contract or an
already JSON-compatible value into a detached plain JSON graph. It returns
containers and scalars, not encoded text; use
`json.dumps(kuma.to_json(value), allow_nan=False)` when text is required. The
conversion supports the public Case, Input, Submission, History, Evidence,
report, Strategy Group, capability, request-record, and batch-result types.
Cycles, more than 256 container levels, non-finite numbers, bytes, sets,
arbitrary dataclasses, subclasses, and unsupported objects fail with
`ValidationError(code="output_invalid")`. The function performs no I/O and is
not a redactor.

## Request recovery

Official Case and Judge starts create a non-secret local record under
`.kuma/requests/` before the first POST. Use `list_requests(repo_path)`,
`show_request(client_request_id, repo_path=...)`, and
`resume_request(client_request_id, repo_path=...)` to inspect or resume it from
a later process. Equivalent commands are `kuma requests list`, `show`, and
`resume`.

The record keeps bounded identity and status metadata, never the API key,
request body, Evidence, Rubric, prompt, or provider response. A known operation
is resumed with GET-only polling. If the original accepted response was lost,
KUMA first performs authenticated lookup using the stable
`kreq_<32 lowercase hex>` client request ID. A prepared record that the Backend
does not know fails as `request_not_started`; KUMA does not invent a bodyless
POST. Successful Judge recovery writes the public report under
`.kuma/reports/<run_id>.json` and retains the terminal request record.

## OpenTelemetry

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
prompts, completions, source, raw logs, credentials, and private Rubrics remain
excluded.

<!-- api-parameters:TraceEvidenceLimits:start -->

| Argument | Type | Required/default | What happens when the limit is reached |
| --- | --- | --- | --- |
| `max_spans` | positive `int` | `200` | After this many ended spans have been retained for a Run, additional spans are dropped and the Evidence reports the drop instead of growing memory without bound. |
| `max_attributes` | positive `int` | `32` | Keeps at most this many safe, allowlisted attributes on each span; additional attributes are dropped and counted. Sensitive attributes remain rejected regardless of this number. |
| `max_events_per_span` | positive `int` | `20` | Keeps at most this many safe OTel events on each span; later events are dropped and reported. |
| `max_text_length` | positive `int` | `256` characters | Truncates each retained allowlisted text value to this many Unicode characters and records that truncation occurred. |
| `max_total_bytes` | positive `int` | `512000` bytes | Caps the compact JSON size of all committed Trace envelopes in one Run. KUMA drops or truncates Trace data to stay within this budget; the value must still fit the smallest valid envelope. |
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
| `TestReport` | `report_id`, `run_id`, `status` (`pass`, `issue`, or `insufficient_evidence`), confidence, stop reason, public issues/evidence gaps, and extensions. |
| `CaptureStatus` | Completeness for file snapshot/diff, logs, sensitive scan, and traces. Each component is `complete`, `partial`, `failed`, or `skipped`. |

Private Rubrics, prompts, model settings, and Core records are not part of these
objects. Runtime Evidence v1 is hash-only; explicitly negotiated v2 may carry a
bounded, scanned completed Agent output. Both formats are defined in the
[Runtime Evidence contract](runtime-evidence.md).

## Error fields

Catch `KumaError` for normal SDK failures. `str(exc)` is a safe user-facing
message. Program logic should use `exc.code`, `exc.retryable`, and
`exc.request_id`; `exc.details` is a bounded public mapping and should be logged
only through an application-approved allowlist.
