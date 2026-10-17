# KUMA Python SDK guide

For active operation interval revisions, bounded backoff and strict timeout
behavior, see [Operation polling and deadlines](operation-polling.md).

[English](sdk-guide.md) | [Chinese overview](../README.zh-CN.md)

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
python -m pip install --upgrade kuma-defuzex
```

Linux or macOS:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --upgrade kuma-defuzex
```

Optional OpenTelemetry support:

```bash
python -m pip install --upgrade "kuma-defuzex[otel]"
```

Repository contributors should install this checkout with `python -m pip install -e ".[test,dev]"`.
Observation and correlation APIs are available in KUMA 0.3.0 or later.
Upgrade the published package using the command above; no source checkout is required.

### Configuration compatibility and container upgrades

Official Case + Judge runs validate the public Judge configuration during
`create_run`, before creating runtime state or submitting paid Case generation.
Judge submission reads current limits again; an early check cannot guarantee that
the server configuration will remain unchanged during Agent execution. Custom
providers, loaded Case workflows, and Judge-disabled runs retain their existing
validation order. Resuming a known Judge operation still polls that operation;
it does not start another job or require a fresh discovery request.

Unknown optional Runtime Evidence capability names in a bounded, valid discovery
list are ignored, never copied into uploaded Evidence. Malformed configuration,
invalid known-capability order/dependencies, and explicitly requested unsupported
features still fail closed. Actual Evidence schemas remain strict. No generic
required-version policy or automatic SDK installation is implemented; an optional
server capability alone is not a mandatory-upgrade signal.

If an application pins `kuma-defuzex==0.3.1`, upgrading the host does not upgrade
its Docker image, virtual environment, or running Python process. Update the pin
to the intended published version (for example `kuma-defuzex==0.3.2`), rebuild the
image, recreate the container, and restart long-lived workers/notebook kernels.
Verify **inside the environment that runs the Agent**:

```bash
python -c "from importlib.metadata import version; import kuma; print(version('kuma-defuzex')); print(kuma.__file__)"
```

For Docker Compose, use your application's service name:

```bash
docker compose build --no-cache <agent-service>
docker compose up -d --force-recreate <agent-service>
```

Installing or rebuilding does not replay a Case or Judge request. Resume an
existing recorded operation instead of rerunning an entire paid workflow.

## Local quickstart

To save and execute the same complete Case in another process, use
`run.save_case("case.json")` followed by
`create_run(repo_path=".", case_path="case.json")`.
See [Case files](case-files.md) for the no-overwrite/path limits, explicit origin,
official-server validation, and the distinction between loading and Judge billing.

The CLI quickstart runs a deterministic exact-match check in an SDK-owned temporary directory. It reads no user repository and requires no account, API key, Docker, or network:

```bash
kuma quickstart
```

Use `kuma quickstart --fail-demo` to exercise the deterministic failure path. A complete local `Run` with a custom Case Provider and no Judge is also available:

```bash
python examples/minimal_local.py
```

## Configuration

### Keep evaluation artifacts outside the Agent workspace

`create_run(storage_path=...)` selects an existing external artifact directory
independently of the tracked `repo_path`. Relative storage paths resolve against
`repo_path`, not the process working directory. Equal, ancestor and descendant
roots, filesystem roots, linked/reparse destinations and unsafe SDK subdirectories
are rejected before runtime setup or network calls. Create the directory yourself
first; there is no silent fallback into the repository.

```python
from pathlib import Path
from kuma import create_run

workspace = Path("./agent-workspace").resolve()
artifacts = Path("./evaluation-artifacts").resolve()
artifacts.mkdir(parents=True, exist_ok=True)
run = create_run(
    repo_path=workspace,
    storage_path=artifacts,
    agent_profile_path="agent-profile.md",
    save_local=True,
)
saved_case = run.save_case("case.json")  # artifacts/case.json, never overwritten
```

Runtime directories, request/recovery ledgers and optional
saved submissions/Evidence/reports use `storage_path/.kuma/`. File tracking and
repository metadata still use `repo_path`; external mode creates neither
`repo_path/.kuma` nor a workspace `.gitignore` rule. The active-Run lock remains
at its platform-wide location to preserve container-wide exclusion.
Discovery caching remains in memory. Credentials and
the user-config/update cache retain their existing user-level locations; they
are not Run artifacts or relocated into the Agent workspace.

With external storage, `run.save_case("case.json")` and
`create_run(repo_path=workspace, storage_path=artifacts, case_path="case.json")`
use the artifact directory as their containment root. Absolute Case paths must
also remain inside it; parent directories must exist. Checksums, privacy scanning,
no-overwrite publication and link/mount protection remain enforced. Loading never
regenerates or truncates the Case. `storage_path=None` preserves legacy locations.

Use the same `storage_path` with `list_requests`, `show_request` and
`resume_request`, or `kuma requests list/show/resume --repo-path ... --storage-path ...`.
Report locators are relative to the selected artifact parent. Storage paths do
not enter requests or semantic request hashes; known operations still resume
through GET only. Pure observation already has independent explicit storage:
`observation.save("observation.json", root=artifacts)`; it does not create a Run.

**Storage separation is not Agent isolation.** The integration must independently
protect the artifact directory, complete future-step Case files, ledger,
output/control mounts, environment and history from Agent access. Moving files
alone provides no sandbox or access-control guarantee.

### Official Case difficulty

```python
from kuma import create_run

run = create_run(
    repo_path="/workspace/repo",
    agent_profile_path="/workspace/repo/agent-profile.md",
    difficulty="D2",
)
```

In an authenticated official workflow, `difficulty` requests both problem count
and challenge intensity: `"D0"` injects zero problems; `"D1"` (default) injects
one obvious, low-intensity problem; `"D2"` injects two subtler or composed problems
that require stronger recognition, recovery and verification. For example, D1
may present one clearly signaled recoverable obstacle, while D2 may combine two
less obvious obstacles that require checking the recovery result.
It does not select a Strategy Group or change Judge severity. `max_steps` remains
the same upper bound: D2 does not request an extra step. Necessary inputs and
solvability must be preserved; difficulty is not a measured failure-rate promise.
Only these exact strings are valid; `None`, lowercase strings and other values
raise `ConfigurationError(code="config_invalid")` before filesystem/network work.
The SDK forwards the choice; Core implements injection, and the SDK does not
claim that generated content has been independently verified.

Direct provider users can set
`OfficialCaseProvider(client, difficulty="D2").generate_case(context)`.
Both this synchronous Python call and `create_run` use the same asynchronous
Backend operation and polling path. Omitted or explicit D1 preserves the previous
request bytes; D0/D2 change the request hash. Retry with the same level preserves
pending-operation identity and uses GET-only polling once an operation is known.
Custom providers, local observation and loaded Case content are unchanged.
Non-default levels require a service supporting the difficulty field; unsupported
requests are not silently retried at D1.

### Alternate official Case generator

`high_difficulty` is a strict boolean, default `False`. Set it to `True` to
request the service-managed alternate Case generator:

```python
from kuma import create_run

run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
    high_difficulty=True,
    difficulty="D1",
)
```

Direct provider usage is `OfficialCaseProvider(client, high_difficulty=True)`;
call `.generate_case(context)` with the usual validated context.
This choice is independent of `difficulty`: both booleans work with D0, D1 and
D2. It does not force D2, add steps, or change Judge behavior. Custom providers
and saved Cases are unchanged.

Omitted or explicit `False` preserves historical request bytes and recovery
identity. `True` changes the request identity; retry with the same choice to
resume the same operation (GET-only once its operation ID is known). Changing
the choice is a different request, not a way to resume the original.
Non-booleans, including `None`, `0`, `1` and `"true"`, fail locally with
`ConfigurationError(code="config_invalid")` before I/O. The service must support
this option; errors are surfaced without retrying on the default generator.
The SDK sends only this boolean, never provider model settings or credentials.

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
| `KUMA_BASE_URL` | Set the final public or loopback API base URL; non-loopback URLs must use HTTPS. Authenticated requests never follow redirects, including same-origin redirects. A redirect raises non-retryable `ServiceError(code="http_redirect_rejected")`; correct the URL rather than retrying the redirecting alias. |

### Agent Profile file

The official Case Provider requires an explicit UTF-8 Agent Profile file with YAML front matter and three sections:

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

The Strategy Group remains the primary testing contract: it determines the
testing capability, domain, and method. The Agent Profile only describes the
Agent under test, production scenario, expected behavior, and prohibited
boundaries so the chosen group has relevant context. Profile prose never selects
or overrides a group. An explicit `strategy_group` coordinate is authoritative;
without one, `strategy="auto"` uses the catalog's exact default. Automatic matching is
disabled.

### Strategy Groups and Agent capabilities

Authenticated users can inspect the current validated public Strategy Group catalog before editing an Agent Profile:

```bash
kuma strategies list
kuma strategies list --output strategy-groups.json
```

Add the selected group `id` and exact `version` through the closed `strategy_group` front-matter object. Omitting it uses the catalog's exact default; an invalid explicit selection or missing Evidence capability fails closed. Keep `scan_strategy_group=False`: `True` raises `ConfigurationError(config_invalid)` before file or network I/O. Automatic matching is disabled, not privacy scanning or capability validation. See [Strategy Groups](strategy-groups.md) for the Agent Profile schema, CLI options, typed Python API, default behavior, and privacy boundary.

An optional `tool_capabilities` relative path links a reviewed capability document. Create or validate it with `kuma tools scan` / `kuma tools validate`, or equivalent Python helpers. Linking opts into official CaseGen uploading the complete normalized document, including schema descriptions/defaults/examples but never its local path. Omission sends no declaration. It is user-declared context, not verified Evidence or automatic group matching. Sensitive content fails before network I/O even with `allow_sensitive=True`; unsupported servers fail without dropping the field. See [Agent tool capabilities](agent-tool-capabilities.md) for schema, bounds, CLI, Python API, and path rules.

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

Complete environment-reference assignment values such as `PASSWORD=${DB_PASSWORD}`
or `PASSWORD="${DB_PASSWORD}"` are symbolic instructions, not embedded credentials.
The same whole `${DB_PASSWORD}` scalar is accepted in a JSON credential field.
Names must match `[A-Z_][A-Z0-9_]*`. KUMA does not read or expand the environment
reference. Attached literals, concatenated references, default-value operators,
and malformed references are not exempted; adjacent real credentials are still
scanned and redacted. Official signed Cases are validated unchanged, never
client-redacted to bypass rejection.

Each `get_input()` to `submit()` interval is one Evidence transaction. Evidence commits only after the immutable Submission is appended to History; a failed submission build does not advance log offsets or Trace budgets.

- Repository metadata is bounded and contains paths, types, sizes, and a fingerprint—not repository file contents.
- File tracking records hashes, sizes, modes, and change types by default. Text content enters Evidence only when `upload_diff=True`.
- `submit(..., logs=[...])` reads increments only from explicitly selected files and requires Evidence capture to be enabled.
- `save_local=True` writes structured records under `.kuma/runs/<run_id>/submissions/`; local persistence does not replace official submission.
- `CaptureStatus`, `missing`, `dropped_count`, and `runtime_warnings` expose partial or degraded capture.

Framework-neutral runtime metadata follows the [Runtime Evidence contract](runtime-evidence.md). v1 remains hash-only; only an explicitly negotiated output-capable official service receives a scanned completed Agent output. Canonical Agent output is capped at 4 MiB, one Runtime Evidence item at 5 MiB, and the complete official multipart body at 8 MiB. Nothing is truncated. That page is the authoritative schema and privacy reference.

Before official upload, KUMA scans output, errors, paths, diffs, explicit logs, and custom Cases for sensitive material. The API key is used for authorization and is not added to Evidence. `allow_sensitive=True` is an explicit ordinary-Evidence override, not a substitute for isolation or secret hygiene.

Both the default Judge and an explicitly supplied `OfficialJudgeProvider` check
Agent output before committing the step, including when `allow_sensitive=True`.
If `SensitiveDataError` is raised, that input remains delivered: remove the secret
and call `submit()` again. No new history entry or Judge POST was created by that
rejected submission. Arbitrary custom Judge providers retain their local policy.

The scanner preserves bounded, non-executed source references, including:
`db_password = os.environ.get("PGPASSWORD", "")` (also without the empty default),
the keyword fragment `password=db_password,`, and
`private_key = Path(tmpdir) / "private.pem"`. Environment names must be uppercase
identifiers; defaults must be absent or empty. Exact `your_password` and `...`
assignment placeholders are supported, as is a body-free PEM construction using
escaped newlines and `"\\n".join(lines)`. These are syntax recognition rules, not
permission to execute source. Additional recognized forms are credential getter
calls with name/attribute arguments, credential references joined by `or`,
`use_*` boolean keyword options, and an Authorization scheme concatenated with
a recognized reference (for example, `"Bearer " + get_api_key()`).
Source lines are limited to 4096 characters and 256 AST nodes; only individual
reference spans are exempted, never the entire statement. The parser executes
nothing. Arbitrary calls, literal getter arguments, nonempty defaults,
secret-bearing concatenations, real/incomplete PEM data, adjacent secrets and JSON
credential-field values remain sensitive. Recognized source in an ordinary JSON
output field is preserved; a credential field is not treated as source code.
Whole `your_*`, ellipsis and truncated-prefix markers such as `hf_...` are
recognized in text assignments. Complete inline PEM marker pairs separated only
by an ellipsis describe syntax, not key material. Real PEM bodies still fail.
Numeric password/secret values remain sensitive, including structured fields;
`password=123456` is not a harmless numeric option. Known provider key shapes
(including Stripe underscore keys) remain detected anywhere beside safe source.

Encoded JSON containers are inspected through at most eight nested decoding
layers. A remaining object/array-shaped string at that budget is not considered
fully scanned, even if its contents might be harmless: it produces the safe
`validation_failed` finding rather than relying on escaped raw text. Reduce the
nested encoding or use the negotiated output-redaction path, which replaces the
uninspected scalar and records content loss. The exact `[REDACTED]` marker is not
a container. Original caller data is never modified; arbitrary encodings are not
decoded or executed to guess whether a secret exists.

For completed official submissions, recognized sensitive Agent output is redacted
only when the service advertises the existing `redaction` capability with named
Runtime Evidence. Without cached discovery, it may perform one existing
`GET /sdk/judge/config/` before step commit, with no Evidence body;
it never starts a Judge operation. The same detached sanitized output is used for
history, local records, hashes and upload, accompanied by a component-scoped
`agent_response_claim` redaction record. Privacy capture is partial with
`output_redacted`; this is not proof of complete claims or task execution. Caller
objects and original user files are unchanged. Unsupported services keep the
existing precommit rejection; a later loss of support blocks POST rather than
silently dropping the annotation. Residual secrets are always rejected, regardless
of `allow_sensitive`. Custom Judge content policy is unchanged.

Authorization examples are preserved only when their complete Bearer/Basic value is a recognized placeholder (such as `$TOKEN`, `${ACCESS_TOKEN}`, `YOUR_TOKEN_HERE`, or `<your-access-token>`) or the public RFC 7617 Basic sample. Supported boundaries are the line end or an immediately enclosing, matching single quote, double quote, or backtick, followed by whitespace or the line end; JSON-escaped double quotes are also supported. Two unquoted documentation forms are recognized: `Example: Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l (RFC 7617 sample)` and `Set the header to Authorization: Bearer YOUR_TOKEN_HERE before calling.` Other prose suffixes, malformed boundaries, unknown schemes and extra material inside a header remain sensitive. Non-exempt headers are redacted from `Authorization` through the line end; surrounding text and additional headers still undergo credential checks.

The offline contributor regression is `python tools/verify_privacy_authorization.py`.
It covers documentation/curl examples, malformed boundaries, adjacent credentials,
Digest and unknown schemes, plus redaction followed by rescanning. Private canonical
tests additionally retain whole `${ENV_NAME}` assignment/JSON behavior; adjacent
literal suffixes remain sensitive. Neither check reads environment credentials or
calls a hosted service.

Embedded narrative labels such as `the secret: patience improves the result`,
`per token: query, key, value`, and compound payment/consent authorization
headings are distinguished from standalone credential slots. Bounded word-only
sentence continuations and explicit authorization states (GRANTED/DENIED) are
preserved; arbitrary HTTP schemes and quoted credential values are not.
Diagnostic dotted rule identifiers ending in a credential word are not treated
as a secret slot when followed by explanatory prose.
Token glossary definitions and compound consent/authorization headings are prose,
not automatically credentials. The SDK recognizes a bounded English explanatory
form: an unquoted colon label, a value starting with `a`, `an`, or `the`, at least
five words, ordinary sentence punctuation, and at most 4096 characters on that
line. For `authorization`, the heading must join another term with `and` or `or`
(for example, `Consent and authorization`); a bare `Authorization:` header remains
strict. This is a conservative syntax rule, not a general natural-language or
secret detector. Other formats may still need rewriting as ordinary prose.

Outside the reference/placeholder forms above, quoted values, equals assignments
and structured credential fields such as
`{"token": "..."}` remain sensitive. Real key signatures, private-key material,
and additional credential assignments are scanned independently even alongside
a safe definition. Recognized prose is preserved without a redaction-loss flag;
actual credentials still follow negotiated redaction or pre-upload rejection.

## OpenTelemetry

OpenTelemetry (OTel) is the standard observability API used by Agent frameworks and instrumentation to emit spans. KUMA maps spans that were **actually emitted in the same process** into bounded Evidence. It does not invent Agent activity and is not an OTel Collector, backend, or trace UI.

Install OTel support only when trace capture is needed; the core package does not require it:

```bash
python -m pip install "kuma-defuzex[otel]"
```

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

Span counts, attributes, events, text and total Run bytes are bounded. Ordinary
attributes use a restrictive allowlist; recognized model/tool bodies have separate
bounded redacted projections, not unrestricted prompt/log collection. Credentials
and private evaluation content are not admitted. Explicit submit(output) remains
the portable fallback; omission requires supported Agent/Workflow final output.
Native OTel logs retain safe metadata/hashes; explicit file logs retain their
separate Submission contract. KUMA provides no OTLP receiver, cross-process Trace
capture, Trace UI or storage server. See [OpenInference](openinference.md) and
[explicit cloud observations](cloud-observations.md).

## Docker and runtime security

Official production runs require the SDK and Agent in the same controlled container by default. `allow_local=True` is a development switch, not a sandbox. The user remains responsible for the Agent's file, command, network, resource, and secret permissions.

Build the supplied user-flow example:

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t kuma-user-flow .
```

See the [full-stack user-flow guide](../examples/full_stack/USER_GUIDE.md) for its exact workspace and runtime requirements.

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

HTTP admission errors `invalid_manifest` and `sensitive_content_detected` may
include optional closed `exc.details`: required `rule` and `location`, plus an
optional `file_index` (integer 0–999, never a boolean). Locations are exactly
`logs`, `manifest`, `manifest.files`, `evidence`, or `run_context`. For `logs` and
`evidence`, the index identifies zero-based uploaded-log order; for
`manifest.files`, it identifies manifest-entry order. Other locations cannot
carry an index. Missing details preserve older-server behavior; malformed or
unknown diagnostic fields produce `invalid_response`, without echoing them.

Manifest rules are `manifest_shape`, `manifest_count`, `manifest_entry`,
`manifest_name_association`, `manifest_hash`, `manifest_evidence_type`,
`manifest_closed_entry`, `case_association`, `submission_association`, and
`validation_failed`. Privacy rules are `sensitive_field_name`,
`named_credential_assignment`, `private_key_pem`, `authorization_bearer`,
`authorization_header`, `cookie_header`, `provider_secret_key`,
`github_classic_token`, `github_fine_grained_token`, `slack_token`,
`defuzex_api_key`, and `validation_failed`. The privacy rule identifies the
server's first sorted actual match; `validation_failed` means no specific cause
was attributed. The SDK validates this vocabulary, not the truth of the claim.
No filenames, dynamic JSON paths, matched values or raw uploads are exposed.
These fields do not change error messages, retry decisions or asynchronous
operation contracts. Use the safe HTTP `request_id` for support correlation;
a supplied valid ID is not proof that the server originated it.


When an official generated Case is rejected by the SDK privacy validator, KUMA raises
`ProviderError(code="service_generated_sensitive_data", retryable=False)`, not a
caller-input error or generic `invalid_response`. Its closed diagnostic details are
`source="service_generated_case"`, `reasons=("sensitive_content",)`, and
`location="public_case"`. No scanner text, matched values, dynamic paths, or raw
Case content are copied into the exception. Other malformed Case responses still
use `invalid_response`.

The signed Case is not edited or redacted by the client. Local recovery keeps the
original operation and idempotency identity without marking the rejected result
accepted. Repeating the same generation call or `kuma requests resume` for its
known operation only polls with GET; it does not create a new charged generation.
`retryable=False` means repeating validation will not repair the content. Preserve
the request identifier for support; do not change the request or start a new
operation as a workaround. A server-side success may already have incurred usage;
this SDK diagnostic does not refund it or claim to prevent that earlier charge.


`timeout` bounds one public HTTP attempt. `operation_wait_timeout` bounds the complete official single-Case or Judge operation. POST retries reuse a stable idempotency key; only server-declared transient failures are retried within `max_retries`, and `ServiceBusyError` is not retried automatically.

`exc.request_id` is the actual response's `X-Request-ID`, retained only when it is exactly 32 lowercase hexadecimal characters. It may be `None`: the header is optional, invalid or duplicate headers are ignored, and no ID is invented. It is not proof that the server generated the ID, since the server can echo an incoming ID. For an asynchronous failed operation it identifies the failing poll response, not the original start request. JSON-body IDs, private Core IDs, and the local `kreq_…` client recovery ID are never substituted; use `kuma requests list/show` for that separate local identity. Error class, code, and retry decisions are unchanged.

HTTP JSON responses must be objects containing finite JSON values with at most 256 nested
containers (the root object counts as one). Excessive nesting, including parser recursion
failures, raises a terminal `invalid_response` SDK error without exposing the response body.

Correlation also survives rejected JSON/UTF-8, response-size limits (including HTTP error bodies), unexpected HTTP status, and invalid operation start/poll/result schemas. It refers only to the response whose validation failed. Network failures before receiving a response, local persistence failures, and missing-header responses do not inherit an earlier response's ID.

An operation timeout retains bounded recovery metadata without storing credentials, request content, Evidence, or results. Judge retry requires the original Run and History; the high-level API cannot rebuild a lost Run from only `run_id` after process exit.

After the server definitively reports a failed, retryable Judge operation, an
explicit later `run.judge()` starts a new attempt with a fresh idempotency key.
This can incur another charge; KUMA never starts that attempt automatically.
Lost POST responses and interrupted polling are different: they retain the
original key, and a known operation ID is resumed with GET only. The committed
History is unchanged, and failed request records remain available locally.

If `KeyboardInterrupt`, `SystemExit`, or cancellation interrupts Judge, the exception still propagates; KUMA does not swallow it or fabricate a report. If your application catches it and retains the Run, its state is `completed` and `run.judge()` can be called again. An already-started official operation resumes by polling its existing ID, not creating another task. Interrupting local waiting does not cancel the remote operation. This recovery does not apply to forcibly terminating the process.

## Troubleshooting

| Symptom | Action |
|---|---|
| Missing API key | Configure a valid key or use fully local Providers / `judge=False` |
| Agent Profile rejected | Check UTF-8, front matter, required headings, and structured-input schema |
| `DockerRequiredError` | Use one controlled container; enable `allow_local=True` only for trusted development |
| `submit()` returns `None` | Check remaining Inputs, `judge`, `run.state`, and `run.history` |
| `InputProtocolError` (`invalid_run_state`) | Alternate one `get_input()` with one `submit()` and avoid concurrent advancement |
| Sensitive-data rejection | Remove secrets from output, paths, logs, diffs, and custom Cases |
| Operation timeout | Keep the original Run, inspect `retryable`, and retry without changing protocols |
| Missing Trace output | Submit explicit JSON output or install and attach `[otel]` correctly |

## Reference

Official non-pass reports expose optional local
`report.extensions["explanation"]` and `report.extensions["step_explanations"]`
for readable summaries. They preserve the Judge's source findings and never
assign global causes to steps. See [examples and interpretation limits](judge-assessment.md#local-readable-explanations).

- [Architecture](architecture.md)
- [Python API reference](api-reference.md)
- [Strategy Groups](strategy-groups.md)
- [Agent tool capabilities](agent-tool-capabilities.md)
- [Public API contract](api-contract.md)
- [Runtime Evidence contract](runtime-evidence.md)
- [Minimal local example](../examples/minimal_local.py)
- [Single Agent template](../examples/single_agent_template/README.md)
- [Full-stack user-flow example](../examples/full_stack/USER_GUIDE.md)
- [Architecture and security boundaries](architecture.md)

## Final Judge input budget

Use `KumaClient().judge_config().get("judge_input_limit")` to inspect the optional
service limit. When present it is exactly
`{"max_input_bytes": 2097152, "measurement": "final_judge_message_utf8"}`
(the number is illustrative; use the returned value). This measures final system
plus serialized user-message UTF-8 bytes, not uploaded file bytes or tokens.
Private service overhead also counts, so the SDK cannot guarantee that an upload
fits by summing local Evidence sizes. An absent field means an older server did
not advertise this information; existing upload checks remain unchanged.

HTTP 413 `payload_too_large` may expose the same two safe fields through
`LimitExceededError.details`. The SDK never truncates Evidence, retries the paid
operation automatically, or increases a limit. Reduce the Evidence for a new
intentional request; preserve the original request identity when recovering it.

## One identity per logical Case generation

`create_run(..., case_request_id=...)` accepts `kreq_` followed by exactly 32
lowercase hexadecimal digits. Persist one ID per intended generation **before**
calling the SDK; reuse that ID and the same inputs after interruption or success.
For example, create `slot_id = "kreq_" + uuid.uuid4().hex` once in your job setup,
save it in your job record, then use:

```python
from kuma import create_run

# Load the previously saved ID for this logical job; do not regenerate on retry.
slot_id = "kreq_0123456789abcdef0123456789abcdef"
run = create_run(
    repo_path=".", agent_profile_path="agent-profile.md",
    case_request_id=slot_id, judge=False,
)
try:
    first_input = run.get_input(full=True)
finally:
    run.cancel()
```

The same ID reuses the original idempotency key. Once an operation ID is known,
generation recovery only polls that operation, including after a successful or
failed result; it never silently buys a replacement. A **different** ID denotes
a separate intentional generation even with identical inputs. Malformed IDs and
custom/reloaded Case combinations fail before I/O. Changed request, Backend or
credential bindings fail before POST; `create_run` may still need bounded
read-only catalog/config discovery to build the canonical payload. Ledger reads
are necessary to verify bindings. Keep the original repository/storage root.

Direct `OfficialCaseProvider` users pass the same optional field through
`CaseGenerationContext`. Omitting it preserves legacy payload-based recovery;
legacy requests never adopt explicitly identified records. The local closed v1
ledger adds only optional `case_request_identity: "explicit"` to Case records,
not request bodies or public list/show fields. Show/resume preserve it. Older SDK
closed readers may reject such records; downgrade recovery is not promised and
must fail closed. No request content, credentials or signed Case rewriting is
introduced. Existing callers must integrate this option explicitly; installing
the SDK alone does not assign separate identities to their logical job slots.
