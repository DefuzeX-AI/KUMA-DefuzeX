# KUMA 策略组

[English](strategy-groups.md) | [简体中文](strategy-groups.zh-CN.md)

策略组是带版本的公开 Case 生成行为族。创建官方 Case 前，KUMA 会从当前公共目录解析出一个精确策略组；私有计划、Rubric、Prompt 和模型设置不会对外暴露。

选定的策略组决定主要测试能力、领域和方法。Agent Profile 只提供在该选择下使用的被测 Agent 与场景上下文；Profile 中的自然语言不会选择、替换或覆盖策略组。使用 `strategy="auto"` 且未声明策略组、未启用 scanner 时，KUMA 解析目录中的精确默认组；下述显式 `safety-baseline` 模式则随机选一个组。

## 随机选择一个基础安全组

```python
from kuma import create_run

run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
    strategy="safety-baseline",
    max_steps=3,
)
```

每次只生成**一个 Case、返回一个 Run**，不是七个 Case。KUMA 读取目录，验证下面
七个 ID 后等概率抽一组（每组 1/7，不按组内成员数量加权）。每个 ID 必须恰好有
一个 `available: true` 的版本；使用目录返回的精确版本和 release，不猜最新版本，
也不固定示例版本：

- `basic-safety-general`
- `basic-safety-coding`
- `basic-safety-cli`
- `basic-safety-browser`
- `basic-safety-research`
- `basic-safety-workflow`
- `basic-safety-data`

任一 ID 缺失、不可用或同时存在多个可用版本，都会在 Case POST 前报
`strategy_group_invalid`；任一组需要当前 Run 不具备的能力，则报
`strategy_capability_mismatch`，不会静默缩小抽样范围。旧服务不支持组目录时返回
`strategy_group_unsupported`。使用前服务端必须已发布全部七组，列出这些名称不代表
你的服务已支持此模式。

Agent Profile 中显式 `strategy_group` 优先于此模式；没有显式组时，此模式优先于
`scan_strategy_group=True`。默认 `strategy="auto"` 和 custom Provider 保持原行为，
custom Provider 原样收到 strategy 字符串，不强制抽组。SDK 实际发送
`strategy_id="auto"` 及真实闭合 `strategy_group_selection`（source 为 `user`），
不会发送虚构的 `safety-baseline` 组 ID。Profile 正文不会决定抽到哪组。

继续使用原有 `get_input` / `submit` 生命周期。`max_steps=3` 是这一个 Case 的步骤
上限。SDK 不会执行你的 Agent。`judge=True` 仍在最后一次提交时触发一次 Judge；
`judge=False` 关闭自动评判，不产生七次请求的费用倍增。

已创建请求的 HTTP 重试和恢复保留已存储的精确坐标。超时或进程退出后，用原请求 ID
执行 `kuma requests list/show/resume` 或 `resume_request(request_id, repo_path=".")`。
恢复仅 GET（必要时先 lookup），按保存的坐标校验 Case，返回含 `case_id` 的
`RequestRecord`，**不会重建 Run，也不返回 Case 正文**。lookup 不存在时不会创建新 Case。
重新调用 `create_run` 是新的选择尝试，不是自动恢复接口，也可能再次抽中同组。
若最终坐标和 payload 完全一致，仍沿用现有 active-request 匹配复用语义；终态记录
继续保留。不新增身份字段或锁。

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
`available: true` 的条目，版本必须以你当前服务器返回的目录为准。

顶层 `default.id` 与 `default.version` 指向精确默认组。需要可审查的本地副本时，可原子保存同一份已校验 JSON：

```bash
kuma strategies list --output strategy-groups.json
```

`--timeout` 设置公共目录请求的秒级超时，默认 `30.0`。`--base-url` 仅用于获准的公共服务或 loopback 联调；普通用户应保留已配置的默认值。凭证缺失或被拒绝、目录畸形、输出目录无效或写入失败时，命令返回非零退出码。

## 在 Agent Profile 中选择策略组

从一个 `available: true` 的 `groups[]` 条目中，原样复制机器可读的 `id`
和 `version` 到 YAML front matter。例如返回的 `basic-safety-coding` 条目版本为 `"1"` 时：

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

这里的 `id` 必须是 `groups[].id` 的精确值，不能填写 `display_name`、列表
序号或策略组内部实际执行的 member strategy；真实 Agent Profile 中也不能
保留占位符。该对象是 closed schema，只接受 `schema_version`、`id` 和
`version`。`selection_source` 与 `catalog_release` 描述校验后的运行时事实，
由 KUMA 在解析当前目录后填充；不要把它们写入 Agent Profile。

显式坐标优先。未知或不可用的策略组会以 `strategy_group_invalid` 直接拒绝；如果 Run 缺少该组 `required_capabilities` 所需能力，则以 `strategy_capability_mismatch` 拒绝并列出缺失项。KUMA 不会为显式选择静默替换其他组。

使用 `strategy="auto"` 且省略 `strategy_group`、未启用 scanner 时，KUMA 使用目录中精确的 `default.id` 与 `default.version`。此选择来源的语义是“general”；`general` 不是固定的策略组 ID。新目录默认指向 `basic-safety-general`，但 SDK 始终读取目录的两个 default 字段，不硬编码。

上文七个 Basic Safety ID 都带完整 `basic-safety-` 前缀。展示名如 “Basic Safety Coding”
不是配置值；没有新增 `category` 字段或 SDK 别名映射。这只是命名调整，不代表安全认证
或测试内容重做。只能选择当前服务器广告的坐标；文档本身不激活目录，历史 release/replay
由服务端保留。完整文件见 [Agent Profile 示例](../examples/basic-safety-agent-profile.md)。

## 可选的本地保守建议

建议功能默认关闭。官方 Run 必须显式开启：

```python
from kuma import create_run

run = create_run(
    repo_path=".",
    agent_profile_path="agent-profile.md",
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

`--catalog` 和 `--capabilities` 为必填；`--output` 可省略，省略后会在终端输出可直接用于 Agent Profile 的 `{schema_version, id, version}` 对象。选择前会校验本地目录与能力文档。能力文件格式见 [Agent 工具能力](agent-tool-capabilities.zh-CN.md)。

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

查询目录和解析官方策略组均需要鉴权。本地建议不会上传能力文件、工具名称、参数 Schema、资源范围、路径、Agent 配置或原始 Agent Profile。创建官方 Case 时只发送解析后的公开坐标、目录版本标识和低敏感度选择来源。

如果旧版公共服务不支持带版本策略组，显式声明会直接失败，不会改变用户意图。省略选择时，可以使用 SDK 严格校验后支持的旧版兼容行为。
