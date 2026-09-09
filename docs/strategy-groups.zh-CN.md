# KUMA 策略组

[English](strategy-groups.md) | [简体中文](strategy-groups.zh-CN.md)

策略组是带版本的公开 Case 生成行为族。创建官方 Case 前，KUMA 会从当前公共目录解析出一个精确策略组；私有计划、Rubric、Prompt 和模型设置不会对外暴露。

选定的策略组决定主要测试能力、领域和方法。Agent Profile 只提供在该选择下使用的被测 Agent 与场景上下文；Profile 中的自然语言不会选择、替换或覆盖策略组。若未声明策略组，KUMA 会解析目录中的精确默认组。

## 查询公共目录

先通过 `KUMA_API_KEY` 配置官方 Key，再运行：

```bash
kuma strategies list
```

该命令执行带鉴权的目录读取，校验完整响应后输出规范 JSON。每个 `groups[]` 条目代表一个可以精确选择的坐标，包含：

- `id` 与 `version`：写入 Agent Profile 的精确坐标；
- `display_name` 与 `description`：公开名称和用途；
- `available`：是否允许新选择；
- `required_capabilities`：Run 必须支持的 Runtime Evidence 能力；
- `limits.max_steps` 与 `limits.supported_difficulties`：公开执行边界。

版本不会嵌套为单独列表。如果同一个策略组 ID 有多个可选版本，响应会
用多条 `groups[]` 记录表示：它们的 `id` 相同、`version` 不同。只能选择
`available: true` 的条目。例如当前目录中的 `BASE-01` 可用版本为 `"1"`。

顶层 `default.id` 与 `default.version` 指向精确默认组。需要可审查的本地副本时，可原子保存同一份已校验 JSON：

```bash
kuma strategies list --output strategy-groups.json
```

`--timeout` 设置公共目录请求的秒级超时，默认 `30.0`。`--base-url` 仅用于获准的公共服务或 loopback 联调；普通用户应保留已配置的默认值。凭证缺失或被拒绝、目录畸形、输出目录无效或写入失败时，命令返回非零退出码。

## 在 Agent Profile 中选择策略组

从一个 `available: true` 的 `groups[]` 条目中，原样复制机器可读的 `id`
和 `version` 到 YAML front matter。例如当前 Security 策略组是
`CAND-007@1`：

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

这里的 `id` 必须是 `groups[].id` 的精确值，不能填写 `display_name`、列表
序号或策略组内部实际执行的 member strategy；真实 Agent Profile 中也不能
保留占位符。该对象是 closed schema，只接受 `schema_version`、`id` 和
`version`。`selection_source` 与 `catalog_release` 描述校验后的运行时事实，
由 KUMA 在解析当前目录后填充；不要把它们写入 Agent Profile。

显式坐标优先。未知或不可用的策略组会以 `strategy_group_invalid` 直接拒绝；如果 Run 缺少该组 `required_capabilities` 所需能力，则以 `strategy_capability_mismatch` 拒绝并列出缺失项。KUMA 不会为显式选择静默替换其他组。

省略 `strategy_group` 时，KUMA 使用目录中精确的 `default.id` 与 `default.version`。此选择来源的语义是“general”；`general` 不是固定的策略组 ID。

## 自动匹配已禁用

保持 `scan_strategy_group=False`（默认值）。传 `True` 会在文件读取或网络前
抛出 `ConfigurationError(code="config_invalid")`，即使已显式选组或使用
Custom Provider 也会拒绝。`strategy="auto"` 且 Profile 未显式选组时，KUMA
始终使用目录精确默认坐标，不根据工具元数据或 Evidence 能力选择其他组。

`kuma strategies suggest` 同样已禁用，返回非零退出码及安全说明；旧
`--catalog`、`--capabilities`、`--output` 参数不会触发文件读写。
需要非默认组时，用 `kuma strategies list` 查询目录后显式声明。

隐私扫描与 [Agent 能力校验](agent-tool-capabilities.zh-CN.md)仍然保留。
声明的 `evidence_types` 与 Run 内在能力仍用于检查选定组的
`required_capabilities`，不会用来选组。底层
`resolve_strategy_group(scan=True)` 也返回相同配置错误。历史
`selection_source="scanner"` wire 仍可解析用于恢复，不为新请求执行匹配。

## Python API

无需创建 Run 即可取得严格类型化目录：

```python
from kuma import KumaClient

catalog = KumaClient().strategy_group_catalog()
print(catalog.default.id, catalog.default.version)

for group in catalog.groups:
    print(group.id, group.version, group.available, group.limits.max_steps)
```

`KumaClient.strategy_group_catalog()` 使用 client 的 API Key、公共 Base URL、超时和可选 transport。它执行一次带鉴权的公共读取并返回 `StrategyGroupCatalog`；畸形或旧格式数据会抛出 `ValidationError`，不会作为可信目录返回。

公开不可变类型包括 `StrategyGroupDeclaration`、`StrategyGroup`、`StrategyGroupCatalog` 与 `ResolvedStrategyGroup`。公开校验函数 `validate_strategy_group_declaration()`、`validate_strategy_group_catalog()` 和 `validate_strategy_group_wire_selection()` 分别校验并分离对应的 closed 对象。精确合同见 [Python API 参考](api-reference.zh-CN.md#策略组-api)。

## 隐私与兼容性

查询目录和解析官方策略组均需要鉴权。KUMA 不会上传能力文件、工具名称、参数 Schema、资源范围、路径、Agent 配置或原始 Agent Profile。创建官方 Case 时只发送解析后的公开坐标、目录版本标识和低敏感度选择来源。

如果旧版公共服务不支持带版本策略组，显式声明会直接失败，不会改变用户意图。省略选择时，可以使用 SDK 严格校验后支持的旧版兼容行为。
