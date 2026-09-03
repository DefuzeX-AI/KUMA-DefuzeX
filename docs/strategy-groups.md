# KUMA Strategy Groups

[English](strategy-groups.md) | [简体中文](strategy-groups.zh-CN.md)

Strategy Groups are versioned public Case-generation behavior families. KUMA resolves one exact group from the current public catalog before an official Case is created; private plans, rubrics, prompts, and model settings are not exposed.

## Query the public catalog

Configure the official key through `KUMA_API_KEY`, then run:

```bash
kuma strategies list
```

The command performs an authenticated catalog read, validates the complete response, and prints canonical JSON. Each `groups[]` entry is one exact selectable coordinate and contains:

- `id` and `version`: the exact coordinate used in a Requirement;
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

## Select a group in a Requirement

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
and never leave placeholder text in a real Requirement. The object is closed:
only `schema_version`, `id`, and `version` are accepted. `selection_source` and
`catalog_release` describe validated runtime facts, so KUMA fills them after
resolving the current catalog; they must not be placed in the Requirement.

An explicit coordinate has priority. An unknown or unavailable group fails closed with `strategy_group_invalid`; a group whose `required_capabilities` are not available fails with `strategy_capability_mismatch` and lists the missing capabilities. KUMA never silently substitutes another group for an explicit choice.

When `strategy_group` is omitted, KUMA uses the catalog's exact `default.id` and `default.version`. The selection source is semantically “general”; `general` is not a fixed group ID.

## Optional conservative local suggestion

Suggestion is disabled by default. Enable it explicitly for an official Run:

```python
from kuma import create_run

run = create_run(
    repo_path=".",
    requirement_path="requirement.md",
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

`--catalog` and `--capabilities` are required. `--output` is optional; without it, the requirement-ready `{schema_version, id, version}` object is printed. The local catalog and capability document are validated before selection. See [Agent tool capabilities](agent-tool-capabilities.md) for the capability-file schema.

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

Catalog discovery and official group resolution require authentication. The local suggestion path does not upload the capability file, tool names, argument schemas, resource scopes, paths, Agent configuration, or raw Requirement. Official Case creation sends only the resolved public coordinate, catalog release, and low-sensitivity selection source.

If an older public service does not support versioned Strategy Groups, an explicit declaration fails rather than changing user intent. Omitted selection may use the strictly validated legacy behavior supported by the SDK.
