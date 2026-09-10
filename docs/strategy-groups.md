# KUMA Strategy Groups
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


[English](strategy-groups.md) | [简体中文](strategy-groups.zh-CN.md)

Strategy Groups are versioned public Case-generation behavior families. KUMA resolves one exact group from the current public catalog before an official Case is created; private plans, rubrics, prompts, and model settings are not exposed.

The selected Strategy Group controls the main testing capability, domain, and method. An Agent Profile supplies only the Agent and scenario context used within that choice; profile prose never selects, replaces, or overrides the group. If no group is declared, KUMA resolves the catalog's exact default.

## Query the public catalog

Configure the official key through `KUMA_API_KEY`, then run:

```bash
kuma strategies list
```

The command performs an authenticated catalog read, validates the complete response, and prints canonical JSON. Each `groups[]` entry is one exact selectable coordinate and contains:

- `id` and `version`: the exact coordinate used in an Agent Profile;
- `display_name` and `description`: its public name and purpose;
- `available`: whether it accepts new selections;
- `required_capabilities`: Runtime Evidence capabilities the Run must support;
- `limits.max_steps` and `limits.supported_difficulties`: public execution bounds.

Versions are not grouped into a nested list. If one group ID has multiple
selectable versions, the response contains multiple `groups[]` entries with the
same `id` and different `version` values. Always choose an entry whose
`available` value is `true`. In the current catalog, for example, `BASE-01` is
available at version `"1"`.

The top-level `default.id` and `default.version` identify the exact default group. Save the same validated JSON atomically when you need a reviewable local copy:

```bash
kuma strategies list --output strategy-groups.json
```

`--timeout` sets the public catalog request timeout in seconds and defaults to `30.0`. `--base-url` is intended only for an authorized public service or loopback integration; ordinary users should keep the configured default. A missing or rejected credential, malformed catalog, invalid output parent directory, or failed write returns a non-zero exit code.

## Select a group in an Agent Profile

Choose one available `groups[]` entry and copy its machine-readable `id` and
`version` exactly into the YAML front matter. For example, the current Security
group is `CAND-007@1`:

```yaml
---
agent_description: A repository maintenance agent
input_type: text
strategy_group:
  schema_version: kuma.strategy_group_selection.v1
  id: CAND-007
  version: "1"
---
```

Here `id` means the exact `groups[].id` value. Do not use `display_name`, a
numbered list position, or the member strategy that the group runs internally,
and never leave placeholder text in a real Agent Profile. The object is closed:
only `schema_version`, `id`, and `version` are accepted. `selection_source` and
`catalog_release` describe validated runtime facts, so KUMA fills them after
resolving the current catalog; they must not be placed in the Agent Profile.

An explicit coordinate has priority. An unknown or unavailable group fails closed with `strategy_group_invalid`; a group whose `required_capabilities` are not available fails with `strategy_capability_mismatch` and lists the missing capabilities. KUMA never silently substitutes another group for an explicit choice.

When `strategy_group` is omitted, KUMA uses the catalog's exact `default.id` and `default.version`. The selection source is semantically “general”; `general` is not a fixed group ID.

## Automatic matching is disabled

Keep `scan_strategy_group=False` (the default). Passing `True` raises
`ConfigurationError(code="config_invalid")` before file or network I/O, even
with an explicit group or custom provider. With `strategy="auto"` and no
explicit Profile group, KUMA always uses the catalog's exact default coordinate;
it never selects a different group from tool metadata or Evidence capabilities.

`kuma strategies suggest` is also disabled and exits nonzero with a safe
explanation. Legacy `--catalog`, `--capabilities`, and `--output` arguments
are not read or written. Use `kuma strategies list` to inspect the catalog and
declare a group explicitly when you need a non-default choice.

Privacy scanning and [Agent capability validation](agent-tool-capabilities.md)
remain active. Declared `evidence_types` plus intrinsic Run capabilities are
still checked against the selected group's `required_capabilities`; they do
not choose the group. Low-level `resolve_strategy_group(scan=True)` rejects
with the same configuration error. Historical `selection_source="scanner"`
wire remains parseable for recovery, but no new matching is performed.

## Python API

Fetch a strict typed catalog without creating a Run:

```python
from kuma import KumaClient

catalog = KumaClient().strategy_group_catalog()
print(catalog.default.id, catalog.default.version)

for group in catalog.groups:
    print(group.id, group.version, group.available, group.limits.max_steps)
```

`KumaClient.strategy_group_catalog()` uses the client's API key, public base URL, timeout, and optional transport. It performs one authenticated public read and returns `StrategyGroupCatalog`; malformed or legacy data raises `ValidationError` instead of being returned as trusted catalog data.

Public immutable types include `StrategyGroupDeclaration`, `StrategyGroup`, `StrategyGroupCatalog`, and `ResolvedStrategyGroup`. The public validators `validate_strategy_group_declaration()`, `validate_strategy_group_catalog()`, and `validate_strategy_group_wire_selection()` validate and detach their corresponding closed objects. See the [Python API reference](api-reference.md#strategy-group-api) for their exact contracts.

## Privacy and compatibility

Catalog discovery and official group resolution require authentication. KUMA does not upload the capability file, tool names, argument schemas, resource scopes, paths, Agent configuration, or raw Agent Profile. Official Case creation sends only the resolved public coordinate, catalog release, and low-sensitivity selection source.

If an older public service does not support versioned Strategy Groups, an explicit declaration fails rather than changing user intent. Omitted selection may use the strictly validated legacy behavior supported by the SDK.
