# KUMA Strategy Groups

[English](strategy-groups.md) | [简体中文](strategy-groups.zh-CN.md)

Strategy Groups are versioned public Case-generation behavior families. KUMA resolves one exact group from the current public catalog before an official Case is created; private plans, rubrics, prompts, and model settings are not exposed.

The selected Strategy Group controls the main testing capability, domain, and method. An Agent Profile supplies only the Agent and scenario context used within that choice; profile prose never selects, replaces, or overrides the group. With `strategy="auto"` and no group or scanner opt-in, KUMA resolves the catalog's exact default. The explicit `safety-baseline` mode below samples one group instead.

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

An explicit Agent Profile `strategy_group` wins over this mode. Otherwise this
mode takes precedence over `scan_strategy_group=True`. Default `strategy="auto"`
and custom providers remain unchanged; custom providers receive the strategy
string without official sampling. The SDK sends member `strategy_id="auto"` plus
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
`available` value is `true`, and use the version returned by your current server.

The top-level `default.id` and `default.version` identify the exact default group. Save the same validated JSON atomically when you need a reviewable local copy:

```bash
kuma strategies list --output strategy-groups.json
```

`--timeout` sets the public catalog request timeout in seconds and defaults to `30.0`. `--base-url` is intended only for an authorized public service or loopback integration; ordinary users should keep the configured default. A missing or rejected credential, malformed catalog, invalid output parent directory, or failed write returns a non-zero exit code.

## Select a group in an Agent Profile

Choose one available `groups[]` entry and copy its machine-readable `id` and
`version` exactly into the YAML front matter. For example, if the returned
`basic-safety-coding` entry has version `"1"`:

```yaml
---
agent_description: A repository maintenance agent
input_type: text
strategy_group:
  schema_version: kuma.strategy_group_selection.v1
  id: basic-safety-coding
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

With `strategy="auto"` and no group or scanner opt-in, KUMA uses the catalog's exact `default.id` and `default.version`. The selection source is semantically “general”; `general` is not a fixed group ID. The renamed catalog defaults to `basic-safety-general`, but both default fields remain catalog-driven.

The seven Basic Safety IDs listed above include the full `basic-safety-` prefix.
Display names such as “Basic Safety Coding” are labels, not configuration values.
There is no new `category` field or alias map. This naming change is not a safety
certification or a redesign of the tests. Use only coordinates advertised by your
server; documentation does not activate a catalog. Historical releases/replays
remain service-owned. See the [complete Agent Profile example](../examples/basic-safety-agent-profile.md).

## Optional conservative local suggestion

Suggestion is disabled by default. Enable it explicitly for an official Run:

```python
from kuma import create_run

run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
    scan_strategy_group=True,
)
```

KUMA compares only the closed Runtime Evidence capability set declared in a reviewed local Agent capability file plus intrinsic Evidence enabled for the Run. It does not run tools or infer capability from tool names, descriptions, schemas, resources, access, or side effects. It selects a non-default group only when one reliable best match exists; a tie or no reliable match uses the catalog default.

To review the same conservative suggestion without creating a Run or making a network request, use previously saved local files:

```bash
kuma strategies suggest \
  --catalog strategy-groups.json \
  --capabilities agent-capabilities.json \
  --output strategy-group.json
```

`--catalog` and `--capabilities` are required. `--output` is optional; without it, the Agent Profile-ready `{schema_version, id, version}` object is printed. The local catalog and capability document are validated before selection. See [Agent tool capabilities](agent-tool-capabilities.md) for the capability-file schema.

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

Catalog discovery and official group resolution require authentication. The local suggestion path does not upload the capability file, tool names, argument schemas, resource scopes, paths, Agent configuration, or raw Agent Profile. Official Case creation sends only the resolved public coordinate, catalog release, and low-sensitivity selection source.

If an older public service does not support versioned Strategy Groups, an explicit declaration fails rather than changing user intent. Omitted selection may use the strictly validated legacy behavior supported by the SDK.
