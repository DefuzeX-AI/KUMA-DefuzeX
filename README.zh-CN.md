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
- 默认 Trace 预算为每 Run 8 MiB；Agent 输出 canonical JSON 上限 4 MiB，
  Runtime Evidence 上限 5 MiB，完整 multipart 上传上限 8 MiB。
  服务端更小的限制仍有效；输出超限会拒绝，不会截断。
- Strategy Group 决定测试方法；Agent Profile 只提供被测 Agent、运行场景、
  预期行为和禁止边界的上下文。

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

## 全栈用户流程示例

按照[全栈用户流程指南](examples/full_stack/README.zh-CN.md)，可在 Docker 中组合运行 KUMA 与 mini-SWE-agent。该流程会调用外部服务，可能消耗服务 Credit 和模型预算。

## 详细文档

[简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [策略组](docs/strategy-groups.zh-CN.md) · [Agent 工具能力](docs/agent-tool-capabilities.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [Agent Profile 迁移说明](docs/migration-agent-profile.md) · [English SDK guide](docs/sdk-guide.md) · [Python API reference](docs/api-reference.md) · [Runtime Evidence 合同](docs/runtime-evidence.md)

## 项目链接

[安全策略](SECURITY.md) · [贡献说明](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
