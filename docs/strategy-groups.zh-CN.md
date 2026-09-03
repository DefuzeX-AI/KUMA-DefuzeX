# KUMA 策略组

[English](strategy-groups.md) | [简体中文](strategy-groups.zh-CN.md)

策略组是带版本的公开 Case 生成行为族。创建官方 Case 前，KUMA 会从当前公共目录解析出一个精确策略组；私有计划、Rubric、Prompt 和模型设置不会对外暴露。

## 查询公共目录

先通过 `KUMA_API_KEY` 配置官方 Key，再运行：

```bash
kuma strategies list
```

该命令执行带鉴权的目录读取，校验完整响应后输出规范 JSON。每个策略组包含：

- `id` 与 `version`：写入 Requirement 的精确坐标；
- `display_name` 与 `description`：公开名称和用途；
- `available`：是否允许新选择；
- `required_capabilities`：Run 必须支持的 Runtime Evidence 能力；
- `limits.max_steps` 与 `limits.supported_difficulties`：公开执行边界。

顶层 `default.id` 与 `default.version` 指向精确默认组。需要可审查的本地副本时，可原子保存同一份已校验 JSON：

```bash
kuma strategies list --output strategy-groups.json
```

`--timeout` 设置公共目录请求的秒级超时，默认 `30.0`。`--base-url` 仅用于获准的公共服务或 loopback 联调；普通用户应保留已配置的默认值。凭证缺失或被拒绝、目录畸形、输出目录无效或写入失败时，命令返回非零退出码。

## 在 Requirement 中选择策略组

把一个可用的目录坐标写入 YAML front matter：

```yaml
---
agent_description: A repository maintenance agent
input_type: text
strategy_group:
  schema_version: kuma.strategy_group_selection.v1
  id: <目录中的策略组 ID>
  version: "<目录中的版本>"
---
```

该对象是 closed schema，只接受 `schema_version`、`id` 和 `version`。`selection_source` 与 `catalog_release` 描述校验后的运行时事实，由 KUMA 在解析当前目录后填充；不要把它们写入 Requirement。

显式坐标优先。未知或不可用的策略组会以 `strategy_group_invalid` 直接拒绝；如果 Run 缺少该组 `required_capabilities` 所需能力，则以 `strategy_capability_mismatch` 拒绝并列出缺失项。KUMA 不会为显式选择静默替换其他组。

省略 `strategy_group` 时，KUMA 使用目录中精确的 `default.id` 与 `default.version`。此选择来源的语义是“general”；`general` 不是固定的策略组 ID。

## 可选的本地保守建议

建议功能默认关闭。官方 Run 必须显式开启：

```python
from kuma import create_run

run = create_run(
    repo_path=".",
    requirement_path="requirement.md",
    scan_strategy_group=True,
)
```

KUMA 只比较经审查的本地 Agent 能力文件声明的 closed Runtime Evidence 能力集合，以及本次 Run 已启用的内在 Evidence。它不会执行工具，也不会根据工具名称、描述、Schema、资源、访问方式或副作用猜测能力。只有存在唯一可靠的最佳匹配时才选择非默认组；同分或没有可靠匹配时使用目录默认组。

如需在不创建 Run、也不联网的情况下审查相同的保守建议，可使用已保存的本地文件：

```bash
kuma strategies suggest \
  --catalog strategy-groups.json \
  --capabilities agent-capabilities.json \
  --output strategy-group.json
```

`--catalog` 和 `--capabilities` 为必填；`--output` 可省略，省略后会在终端输出可直接用于 Requirement 的 `{schema_version, id, version}` 对象。选择前会校验本地目录与能力文档。能力文件格式见 [Agent 工具能力](agent-tool-capabilities.zh-CN.md)。

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

查询目录和解析官方策略组均需要鉴权。本地建议不会上传能力文件、工具名称、参数 Schema、资源范围、路径、Agent 配置或原始 Requirement。创建官方 Case 时只发送解析后的公开坐标、目录版本标识和低敏感度选择来源。

如果旧版公共服务不支持带版本策略组，显式声明会直接失败，不会改变用户意图。省略选择时，可以使用 SDK 严格校验后支持的旧版兼容行为。
