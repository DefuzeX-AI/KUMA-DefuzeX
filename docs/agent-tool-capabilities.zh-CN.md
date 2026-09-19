# KUMA Agent tool capabilities

> This historical documentation path now contains the English guide. The
> [canonical guide](agent-tool-capabilities.md) is maintained alongside it; the
> [Chinese overview](../README.zh-CN.md) remains available at the repository root.

[English](agent-tool-capabilities.md) | [Chinese overview](../README.zh-CN.md)

KUMA can normalize tool metadata explicitly exported by an Agent into a local, versioned, editable JSON document. The feature is optional: you may create the same document manually. In both modes, you own and review the final file referenced by the Agent Profile.

Explicitly linking the document from an Agent Profile opts into sending its complete normalized content to official CaseGen. This includes tool names, argument schemas (descriptions/defaults/examples included), category-only resource scopes, and provenance—not the local file path or unrelated Agent configuration. Omitting the link sends no tool declaration. The document supplies context for the selected Strategy Group; it is not verified Evidence and does not choose or override the group. Declared `evidence_types` still participate in capability preflight, not automatic matching (which remains disabled). See [Strategy Groups](strategy-groups.md).

## Canonical document

`schema_version` must be `kuma.agent_tool_capabilities.v1`:

```json
{
  "provenance": "user_declared",
  "schema_version": "kuma.agent_tool_capabilities.v1",
  "tools": [
    {
      "evidence_types": ["file_change", "tool_call"],
      "input_schema": {
        "additionalProperties": false,
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "type": "object"
      },
      "name": "read_file",
      "read_only": true,
      "resource_scopes": [
        {"access": "read", "resource": "repository"}
      ],
      "side_effects": [],
      "version": "1.0"
    }
  ]
}
```

Document fields:

| Field | Accepted value | Meaning |
| --- | --- | --- |
| `schema_version` | `kuma.agent_tool_capabilities.v1` | Closed schema version; unknown versions fail closed. |
| `provenance` | `user_declared` or `scanner_generated` | Records origin, not independently verified tool behavior. |
| `tools` | 1–100 unique entries | Canonically ordered by `name` and `version` when saved. |

Each tool requires exactly these fields:

| Field | Accepted value | Meaning |
| --- | --- | --- |
| `name` | non-empty string, at most 128 characters | Displayed or registered tool name; KUMA does not verify implementation. |
| `version` | string up to 64 characters, or `null` | Published tool contract version. |
| `input_schema` | offline-valid JSON Schema object | Complete tool-argument schema; `$ref`, `$dynamicRef`, and `$recursiveRef` must use internal `#` references. |
| `read_only` | boolean | Must not conflict with a state-changing side effect. |
| `side_effects` | unique subset of `filesystem_write`, `process_execution`, `network_access`, `external_state_change` | Explicit user/scanner claim about effects. |
| `resource_scopes` | objects containing closed `resource` and `access` values | Low-sensitivity categories only; never put paths, hosts, or credentials here. |
| `evidence_types` | unique subset of the seven supported Runtime Evidence capabilities | Evidence the integration can emit; declaring it does not create Evidence. |

Supported `resource` values are `repository`, `workspace`, `temporary_directory`, `process`, `network`, and `external_service`. Supported `access` values are `read`, `write`, `execute`, and `connect`. Supported Evidence capabilities are `file_change`, `tool_call`, `command_result`, `test_result`, `state_transition`, `artifact_snapshot`, and `agent_response_claim`.

Unknown fields, duplicate tool coordinates, non-finite numbers, external schema references, excessive nesting, oversized files, and sensitive content are rejected. There is no `allow_sensitive` override for capability documents.

The canonical document is at most **262144 UTF-8 bytes**, measured as
`json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"`.
Maximum JSON depth is 32 (root depth 0). Each tool has at most 20 unique resource
scopes. Tools sort by `(name, version or "")`; scopes by `(resource, access)`;
side effects and Evidence types sort lexicographically. Nothing is truncated.
Ordinary schema property names such as `token`, `input_tokens`, `password`, and
`api_key` are allowed; actual credentials in any description/default/example
are not. Private evaluation fields are also rejected before upload. Malformed
documents raise `ValidationError(code="tool_capabilities_invalid")`; sensitive
documents raise `SensitiveDataError(code="sensitive_data_blocked")` without
returning the matched value. These checks precede official discovery and POST.

## Create or validate from the CLI

The scanner reads only one explicitly selected inert JSON manifest. It does not import the Agent, discover modules, traverse the repository, execute tools, or access the network:

```bash
kuma tools scan exposed-tools.json --output agent-capabilities.json
kuma tools validate agent-capabilities.json
```

`scan` atomically writes a canonical document with `scanner_generated` provenance. Its terminal summary contains only schema version, provenance, tool count, and destination path. Review and edit the file before use. `validate` checks an existing manual or generated document locally and does not submit it.

## Python API

```python
from kuma import save_agent_capabilities, scan_agent_tools

draft = scan_agent_tools(agent_exposed_tool_mappings)
editable = draft.to_dict()
# Review or edit the plain mapping before saving it.
path = save_agent_capabilities(editable, "agent-capabilities.json")
```

`scan_agent_tools()` accepts a list or tuple of plain mappings—not callables or framework objects—and returns immutable `AgentCapabilities`. `validate_agent_capabilities()` validates a plain canonical mapping. `load_agent_capabilities()` reads and validates one UTF-8 JSON file. `save_agent_capabilities()` revalidates and atomically writes the destination. `scan_agent_tool_manifest()` reads the closed scanner-input manifest and returns a validated generated document.

Public value types are `AgentCapabilities`, `ToolCapability`, and `ResourceScope`; each provides a detached `to_dict()` representation. All operations are local and never execute an Agent tool.

## Link the file from a Agent Profile

Keep the file inside the Agent Profile directory and use a relative path:

```yaml
---
agent_description: A repository maintenance agent
input_type: text
tool_capabilities: agent-capabilities.json
---
```

KUMA validates the document before Provider I/O. Absolute paths, parent-directory escapes, and links resolving outside the Agent Profile directory fail closed. The resulting `Run` exposes the local association through `run.tool_capabilities_path` and `run.tool_capabilities_provenance`. A custom Case Provider receives the canonical mapping in `CaseGenerationContext.tool_capabilities`.

The file is a user-controlled claim. KUMA validates its syntax, bounds, and privacy but does not confirm that a tool exists, is truly read-only, or emits the declared Evidence.

Use `create_run(repo_path=".", agent_profile_path="agent-profile.md")` with a
complete Agent Profile containing the link above. Official CaseGen sends the
normalized document as top-level `tool_capabilities`. Direct Provider callers
can pass the same mapping as `CaseGenerationContext.tool_capabilities`.
The complete metadata participates in request identity; changing a description
changes the request hash. Recovery retains the original request identity and
does not store tool metadata in the local request ledger. Judge wire is unchanged.
A supporting Backend is required: an older server's rejection is surfaced, never
retried by silently deleting the declaration. There is no additional negotiation
GET. Local `scan`, `validate`, `load`, and `save` alone never upload anything.
