# KUMA Agent 工具能力

[English](agent-tool-capabilities.md) | [简体中文](agent-tool-capabilities.zh-CN.md)

KUMA 可以把 Agent 显式导出的工具元数据规范化为本地、带版本、可编辑的 JSON 文档。此功能可选：你也可以手写同一格式。无论采用哪种方式，最终由用户审查并在 Agent Profile 中引用该文件。

能力文档始终留在本地。KUMA 不上传工具名称、参数 Schema、资源范围、本地路径或工具配置。明确启用本地策略组建议时，只有已声明 `evidence_types` 的规范并集参与匹配。详见[策略组](strategy-groups.zh-CN.md)。

## 规范文档

`schema_version` 必须为 `kuma.agent_tool_capabilities.v1`：

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

文档字段：

| 字段 | 接受值 | 含义 |
| --- | --- | --- |
| `schema_version` | `kuma.agent_tool_capabilities.v1` | Closed schema 版本；未知版本直接拒绝。 |
| `provenance` | `user_declared` 或 `scanner_generated` | 记录来源，不代表工具行为已经被独立验证。 |
| `tools` | 1–100 个唯一条目 | 保存时按 `name` 和 `version` 规范排序。 |

每个工具必须且只能包含以下字段：

| 字段 | 接受值 | 含义 |
| --- | --- | --- |
| `name` | 非空字符串，最多 128 个字符 | 展示或注册的工具名称；KUMA 不验证其实现。 |
| `version` | 最多 64 个字符的字符串或 `null` | 工具公开的合同版本。 |
| `input_schema` | 本地 JSON Schema 对象 | 工具参数 Schema；禁止外部 `$ref`。 |
| `read_only` | 布尔值 | 不得与会修改状态的副作用冲突。 |
| `side_effects` | `filesystem_write`、`process_execution`、`network_access`、`external_state_change` 的唯一子集 | 用户或 scanner 对副作用的显式声明。 |
| `resource_scopes` | 仅含 closed `resource` 与 `access` 值的对象 | 只能使用低敏感度分类；不得写入路径、Host 或凭证。 |
| `evidence_types` | 七种受支持 Runtime Evidence 能力的唯一子集 | 集成能够产生的 Evidence；声明本身不会创建 Evidence。 |

`resource` 可取 `repository`、`workspace`、`temporary_directory`、`process`、`network` 或 `external_service`；`access` 可取 `read`、`write`、`execute` 或 `connect`。Evidence 能力可取 `file_change`、`tool_call`、`command_result`、`test_result`、`state_transition`、`artifact_snapshot` 或 `agent_response_claim`。

未知字段、重复工具坐标、非有限数字、外部 Schema 引用、过深嵌套、超大文件和敏感内容都会被拒绝。能力文档不支持 `allow_sensitive` 覆盖。

## 使用 CLI 创建或校验

Scanner 只读取用户显式选择的一份静态 JSON manifest，不导入 Agent、不发现模块、不遍历仓库、不执行工具，也不联网：

```bash
kuma tools scan exposed-tools.json --output agent-capabilities.json
kuma tools validate agent-capabilities.json
```

`scan` 会原子写入 `scanner_generated` 来源的规范文档。终端摘要只包含 Schema 版本、来源、工具数量和目标路径。使用前请打开并审查或编辑该文件。`validate` 只在本地检查现有手写或生成文档，不会提交内容。

## Python API

```python
from kuma import save_agent_capabilities, scan_agent_tools

draft = scan_agent_tools(agent_exposed_tool_mappings)
editable = draft.to_dict()
# 保存前审查或编辑普通 mapping。
path = save_agent_capabilities(editable, "agent-capabilities.json")
```

`scan_agent_tools()` 只接受由普通 mapping 组成的 list 或 tuple，不接受 callable 或框架对象，并返回不可变的 `AgentCapabilities`。`validate_agent_capabilities()` 校验普通规范 mapping；`load_agent_capabilities()` 读取并校验一个 UTF-8 JSON 文件；`save_agent_capabilities()` 重新校验并原子写入目标；`scan_agent_tool_manifest()` 读取 closed scanner 输入 manifest，并返回已校验的生成文档。

公开值类型包括 `AgentCapabilities`、`ToolCapability` 和 `ResourceScope`，均提供分离后的 `to_dict()` 表示。所有操作都只在本地进行，绝不执行 Agent 工具。

## 从 Agent Profile 引用

能力文件必须位于 Agent Profile 所在目录内，并通过相对路径引用：

```yaml
---
agent_description: A repository maintenance agent
input_type: text
tool_capabilities: agent-capabilities.json
---
```

KUMA 会在 Provider I/O 前校验能力文档。绝对路径、父目录逃逸，以及解析到 Agent Profile 目录外的链接都会直接拒绝。生成的 `Run` 通过 `run.tool_capabilities_path` 和 `run.tool_capabilities_provenance` 暴露本地关联；自定义 Case Provider 可从 `CaseGenerationContext.tool_capabilities` 取得规范 mapping。

能力文件属于用户声明。KUMA 会校验语法、边界和隐私，但不会确认工具真实存在、确实只读或能够产生所声明的 Evidence。
