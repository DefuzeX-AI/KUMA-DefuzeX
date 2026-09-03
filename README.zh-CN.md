<p align="center">
  <img src="docs/assets/kuma-banner.svg" width="760" alt="KUMA geometric wordmark banner">
</p>

<h1 align="center">KUMA</h1>

<p align="center">
  <strong>KUMA Python SDK</strong><br>
  面向 Agent 的知识与证据驱动通用评测
</p>

<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README.zh-CN.md">简体中文</a>
</p>

KUMA 是公开 Python SDK，通过严格的 `Run` 协议和有界 Evidence 采集测试 Agent 行为。官方服务仅通过公开 HTTPS 访问；SDK 不运行 Agent、不执行模型，也不暴露私有评估逻辑。

## 核心能力

- 同步且不绑定框架的 Case 与 Judge 流程。
- 支持官方或自定义 Provider，也可完全本地运行。
- 有界采集文件、日志和可选 Trace Evidence。

## 安装

需要 Python 3.10 或更高版本：

```bash
python -m pip install "kuma-defuzex==0.1.0"
```

## 快速开始

无需账号、API Key、Docker 或网络即可运行确定性的本地检查：

```bash
kuma quickstart
```

## 选择策略组

使用官方服务时，请先配置 `KUMA_API_KEY`，再查询当前公共目录：

```bash
kuma strategies list
kuma strategies list --output strategy-groups.json
```

KUMA 会先校验目录，再显示或保存策略组的 `id`/`version`、`display_name`、`description`、`available`、`required_capabilities`、`limits.max_steps`、`limits.supported_difficulties` 以及精确的 `default` 坐标。把选中的坐标写入 Requirement front matter：

```yaml
strategy_group:
  schema_version: kuma.strategy_group_selection.v1
  id: <目录中的策略组 ID>
  version: "<目录中的版本>"
```

用户只能填写这三个字段；`selection_source` 和 `catalog_release` 由 KUMA 补充。显式选择未知、不可用或能力不满足的策略组时会直接拒绝，不会静默回退。省略 `strategy_group` 时使用目录中的 `default.id` 与 `default.version`；“general”只是语义，不是固定 ID。`scan_strategy_group=True` 只是明确启用本地保守建议。详见[策略组](docs/strategy-groups.zh-CN.md)。

## 全栈用户流程示例

按照[全栈用户流程指南](examples/full_stack/README.zh-CN.md)，可在 Docker 中组合运行 KUMA 与 mini-SWE-agent。该流程会调用外部服务，可能消耗服务 Credit 和模型预算。

## 详细文档

[简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [English SDK guide](docs/sdk-guide.md) · [Python API reference](docs/api-reference.md) · [Runtime Evidence 合同](docs/runtime-evidence.md)

## 项目链接

[安全策略](SECURITY.md) · [贡献说明](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
