# KUMA Agent tool capabilities

[English](agent-tool-capabilities.md) | [简体中文](agent-tool-capabilities.zh-CN.md)

KUMA can normalize tool metadata explicitly exported by an Agent into a local, versioned, editable JSON document. The feature is optional: you may create the same document manually. In both modes, you own and review the final file referenced by the Requirement.

The document remains local. KUMA does not upload tool names, argument schemas, resource scopes, local paths, or tool configuration. When local Strategy Group suggestion is enabled, only the canonical union of declared `evidence_types` participates in matching. See [Strategy Groups](strategy-groups.md).

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
| `input_schema` | local JSON Schema object | Tool-argument schema; external `$ref` values are forbidden. |
| `read_only` | boolean | Must not conflict with a state-changing side effect. |
| `side_effects` | unique subset of `filesystem_write`, `process_execution`, `network_access`, `external_state_change` | Explicit user/scanner claim about effects. |
| `resource_scopes` | objects containing closed `resource` and `access` values | Low-sensitivity categories only; never put paths, hosts, or credentials here. |
| `evidence_types` | unique subset of the seven supported Runtime Evidence capabilities | Evidence the integration can emit; declaring it does not create Evidence. |

Supported `resource` values are `repository`, `workspace`, `temporary_directory`, `process`, `network`, and `external_service`. Supported `access` values are `read`, `write`, `execute`, and `connect`. Supported Evidence capabilities are `file_change`, `tool_call`, `command_result`, `test_result`, `state_transition`, `artifact_snapshot`, and `agent_response_claim`.

Unknown fields, duplicate tool coordinates, non-finite numbers, external schema references, excessive nesting, oversized files, and sensitive content are rejected. There is no `allow_sensitive` override for capability documents.

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

## Link the file from a Requirement

Keep the file inside the Requirement directory and use a relative path:

```yaml
---
agent_description: A repository maintenance agent
input_type: text
tool_capabilities: agent-capabilities.json
---
```

KUMA validates the document before Provider I/O. Absolute paths, parent-directory escapes, and links resolving outside the Requirement directory fail closed. The resulting `Run` exposes the local association through `run.tool_capabilities_path` and `run.tool_capabilities_provenance`. A custom Case Provider receives the canonical mapping in `CaseGenerationContext.tool_capabilities`.

The file is a user-controlled claim. KUMA validates its syntax, bounds, and privacy but does not confirm that a tool exists, is truly read-only, or emits the declared Evidence.
