# Strategy Groups

> This historical documentation path now contains the English guide. The
> [canonical guide](strategy-groups.md) is maintained alongside it; the
> [Chinese overview](../README.zh-CN.md) remains available at the repository root.

## Inspect actual execution

After `create_run(...)` succeeds, inspect `run.executed_strategy_group`:

```python
actual = run.executed_strategy_group
if actual is None:
    print("Execution group unconfirmed (historical server/result or custom Run)")
else:
    print(actual["strategy_group_id"], actual["strategy_group_version"])
    print(actual["catalog_release"])
```

This read-only mapping contains exactly `schema_version` (always
`kuma.executed_strategy_group.v1`), `strategy_group_id`, `strategy_group_version`,
and `catalog_release`. It comes from server-reported actual execution, not a
request echo or current catalog lookup. When a Group was submitted, all three
coordinates must match before the SDK completes the request. Without a submitted
Group, only the returned shape is validated; no request match is implied.
Missing historical data stays `None`, not a guessed default. Malformed/null or
mismatched data raises `ProviderError(code="invalid_response")`; recovery keeps
the original operation and polls by GET, without a second paid POST.

This is validated execution metadata, **not independent cryptographic proof**:
it is outside the existing signed raw Case and is not added to Judge wire.
The public `strategy_id`/`strategy_version` (for example `coding@1`) remain
compatibility coordinates, not disclosure of an actual private strategy member.

## Discovery cache and refresh

Official `create_run` calls share validated catalogs within one process for at
most **60 seconds after successful validation**, with a **32-entry LRU** keyed
by canonical Backend URL and the exact credential fingerprint. Different keys,
URLs and processes do not share entries; custom transports bypass this cache.
The cache contains discovery metadata, not Agent content, request bodies or keys.

`kuma strategies list`, `KumaClient.strategies()` and
`KumaClient.strategy_group_catalog()` always fetch a fresh catalog. A failed
refresh invalidates the previous value; there is no stale-on-error fallback.
Concurrent Run lookups share one GET. Waiters stop after the smaller of their
HTTP timeout and 30 seconds, without starting replacement GETs automatically.
Each Run still checks its selection and required capabilities, even on a hit.

Availability can be up to 60 seconds old; the server remains authoritative.
Refreshing metadata does not change a pending request's catalog release or
idempotency identity, and never automatically retries a paid Case/Judge request.
This optimization removes redundant discovery GETs, not Case/Judge execution:
with the default Judge enabled, the final `run.submit(...)` also waits for Judge.

[English](strategy-groups.md) | [Chinese overview](../README.zh-CN.md)

Strategy Groups are the primary test-selection contract. A group fixes the
versioned testing capability, domain, and method used for Case generation without
exposing private Case plans, rubrics, prompts, or model settings. KUMA resolves
exactly one public catalog coordinate before official Case generation and sends
only that coordinate, its catalog release, and a low-sensitivity selection source.

An Agent Profile has a different job: it describes the Agent under test, its
production scenario, expected behavior, and prohibited boundaries. That context
helps the selected Strategy Group generate relevant Cases, but Profile prose never
selects, replaces, or overrides the group. An explicit `strategy_group` object is
authoritative regardless of the surrounding prose. When it is absent, KUMA uses
the exact catalog default in `auto` mode, or samples one group when the caller
explicitly selects `safety-baseline`. Automatic capability matching is disabled.

## Sample one Basic Safety group

```python
from kuma import create_run

run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
    strategy="safety-baseline",
    max_steps=3,
)
```

This creates **one Case and one Run**, not seven Cases. KUMA reads the catalog,
validates all seven IDs below, and samples one group uniformly (one chance in
seven per group, not weighted by member count). Each ID must have exactly one
`available: true` version; KUMA uses that version and the returned catalog release,
never guesses the latest version or pins an example version:

- `basic-safety-general`
- `basic-safety-coding`
- `basic-safety-cli`
- `basic-safety-browser`
- `basic-safety-research`
- `basic-safety-workflow`
- `basic-safety-data`

If any ID is missing, unavailable, or has multiple available versions, creation
fails with `strategy_group_invalid` before any Case POST. If any group needs
capabilities this Run lacks, it fails with `strategy_capability_mismatch`; KUMA
does not silently narrow the pool. Older services without the Group catalog
return `strategy_group_unsupported`. This requires a service publishing all seven
groups; these names are not a claim that your server already supports the mode.

An explicit Agent Profile `strategy_group` wins over this mode. Keep
`scan_strategy_group=False`; `True` is disabled and raises a configuration error
even with an explicit group or this mode. Default `strategy="auto"`
and custom providers remain unchanged; custom providers receive the strategy
string without official sampling. The SDK sends compatibility `strategy_id="auto"` plus
the actual closed `strategy_group_selection` with source `user`, never a fictional
`safety-baseline` Group ID. Profile prose does not pick the sampled group.

Continue with the existing `get_input` / `submit` lifecycle. `max_steps=3` is the
upper bound for this one Case. The SDK does not execute your Agent. Existing
`judge=True` behavior is unchanged: the final submission triggers one Judge;
`judge=False` disables automatic judging. There is no seven-request multiplier.

HTTP retries and recovery of an existing request preserve its stored exact
selection. Use `kuma requests list/show/resume` or `resume_request(request_id,
repo_path=".")` with the original request ID after a timeout or process loss.
Recovery performs GET only (lookup first when needed), validates the returned
Case against the saved coordinate, and returns `RequestRecord` with `case_id`,
**not a reconstructed Run or Case body**. A lookup miss never starts a new Case.
Another `create_run` is a new selection attempt, not an automatic recovery API;
it can sample the same group again. Existing matching-final-payload active-request
reuse still applies when the selected coordinate and payload are identical.
Terminal records are retained. No new identity fields or locks are introduced.

## Declare a group explicitly

Run `kuma strategies list` first and copy an available entry's exact `id` and
`version` into Agent Profile YAML front matter. For example, if the returned
coding entry has version `"1"`:

```yaml
---
agent_description: A repository maintenance agent.
input_type: text
strategy_group:
  schema_version: kuma.strategy_group_selection.v1
  id: basic-safety-coding
  version: "1"
---
```

Only `schema_version`, `id`, and `version` are accepted. The user cannot set
`selection_source` or `catalog_release`; KUMA obtains those from the validated
service catalog. An unavailable/unknown coordinate fails with
`strategy_group_invalid`. If the selected group needs Evidence the current Run
cannot produce, creation fails before Case generation with
`strategy_capability_mismatch`; `error.details["missing_capabilities"]` lists the
missing values in canonical order. Explicit selections never fall back.

The renamed Basic Safety IDs are `basic-safety-general`, `basic-safety-coding`,
`basic-safety-cli`, `basic-safety-browser`, `basic-safety-research`,
`basic-safety-workflow`, and `basic-safety-data`. Every ID includes the full
`basic-safety-` prefix. Display names such as “Basic Safety Coding” are labels,
not configuration values; there is no new `category` field or SDK alias map.
This is a naming change, not a safety certification or a redesign of the tests.
See the [complete Agent Profile example](../examples/basic-safety-agent-profile.md).
Use only coordinates advertised by your server; this documentation does not
activate a catalog. Multiple versions appear as separate entries with the same
ID. Historical releases/replays remain service-owned.

## Default and capability preflight

With `strategy="auto"` and no declaration, KUMA always uses the catalog's exact `default.id` and
`default.version` with source `general`. “General” is a meaning, not a hardcoded
group ID. The renamed catalog points to `basic-safety-general`, but the SDK
always reads both default fields from the catalog rather than hardcoding them.

Automatic Strategy Group matching is disabled. `scan_strategy_group=True`
raises `ConfigurationError(code="config_invalid")` before file or network I/O.
Leave it at `False`, use the catalog default, or declare a group explicitly.
This does **not** disable privacy scanning, Agent capability validation, or
the selected group's required-capability preflight. The latter uses the union of:

- `evidence_types` in the reviewed `AgentCapabilities.tools` declaration;
- intrinsic Run Evidence from `track_files` and configured OTel capture.

These capabilities validate the already selected group; they never choose a
different group for `auto`. Declarations remain user-controlled claims, not
verified facts about a running Agent. Tool names or profile prose do not route it.

The exact capability order and closed vocabulary are:

1. `file_change`
2. `tool_call`
3. `command_result`
4. `test_result`
5. `state_transition`
6. `artifact_snapshot`
7. `agent_response_claim`

## Inspect the catalog from the CLI

Fetch the authenticated public catalog:

```bash
kuma strategies list
kuma strategies list --output strategy-catalog.json
```

`kuma strategies suggest` is disabled: it exits nonzero with the same safe
configuration error before reading `--catalog` / `--capabilities` or writing
`--output`. Supplied paths are neither opened nor echoed. Use `strategies list`
and an explicit Agent Profile group instead. Python callers can still fetch the
strict typed catalog with `KumaClient.strategy_group_catalog()`. Low-level
`resolve_strategy_group(..., scan=True)` is disabled as well; `scan=False`
preserves explicit/default selection. Historical `selection_source="scanner"`
wire values remain parseable for recovery; no new request uses matching.

## Compatibility and privacy

### Catalog fields and Python access

Configure KUMA_API_KEY before discovery. Each groups[] entry is one exact
coordinate, with id/version, display_name/description, available,
required_capabilities and limits.max_steps/supported_difficulties. Multiple
versions use separate entries with the same ID, not a nested version list.
Copy only an available entry's machine-readable ID/version: never a display
name, list index, internal member identity or unresolved placeholder.

`strategies list --output` atomically saves the validated JSON. `--timeout`
is seconds (default 30.0); `--base-url` is for an authorized public service or
loopback integration. Missing/rejected credentials, malformed catalogs and
invalid/unwritable output destinations cause nonzero exit status.

```python
from kuma import KumaClient

catalog = KumaClient().strategy_group_catalog()
print(catalog.default.id, catalog.default.version)
for group in catalog.groups:
    print(group.id, group.version, group.available, group.limits.max_steps)
```

This makes one authenticated catalog read using the client's key/base URL/timeout
and optional transport. Malformed or legacy data raises ValidationError, not a
trusted typed catalog. StrategyGroupDeclaration, StrategyGroup,
StrategyGroupCatalog and ResolvedStrategyGroup are immutable public values;
validate_strategy_group_declaration/catalog/wire_selection validate and detach
their corresponding closed forms. See [API reference](api-reference.md#strategy-group-api).

### Service compatibility

Official Case creation performs `GET /sdk/strategies/` even when Judge is
disabled, because Case selection is independent of Judge and Evidence upload.
New catalogs produce top-level Case field `strategy_group_selection`. A legacy
Backend receives the old request only when its complete deployed top-level and
per-strategy shape validates and no structured declaration exists. Extra,
malformed, or private-looking legacy fields fail closed. An explicit declaration
against a legacy Backend fails with `strategy_group_unsupported` rather than
silently changing user intent.

An explicitly linked capability document is sent separately as `tool_capabilities`,
including tool schemas, as context for the selected group—not group matching.
Without a link it is omitted. Local paths, unrelated Agent configuration, raw
Profile, repository contents, and scanner internals are not sent. See
[tool capability privacy](agent-tool-capabilities.md). Transport must be validated against
their independently accepted exact candidates; SDK fixture tests do not claim a
deployed service already supports this contract.
