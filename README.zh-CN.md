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

KUMA 是一个不绑定框架的 Python SDK，用于评测 AI Agent。它将测试 Case
按步骤交付为 Input，采集有界 Evidence，并通过官方或自定义 Provider 生成
Judgment。KUMA 不负责运行被测 Agent，也不公开私有评测逻辑。

## 可以做什么

- 运行可重复的 Case-to-Judgment 评测。
- 使用官方服务或自定义 Case 和 Judge Provider，包括全本地流程。
- 从结果、文件变更、日志和可选 OpenTelemetry Trace 中采集结构化 Evidence。
- 选择带版本的 Strategy Group，并通过 Agent Profile 描述被测 Agent。

## 安装

需要 Python 3.10 或更高版本：

```bash
python -m pip install --upgrade kuma-defuzex
```

安装包名为 `kuma-defuzex`，Python 导入和命令行名称均为 `kuma`。

## 快速开始

无需账号、API Key、Docker 或网络即可运行确定性的本地检查：

```bash
kuma quickstart
```

## 下一步

- [运行官方评测](docs/sdk-guide.md)，完成完整的 Case 与 Judge 流程。
- [在本地观察 Agent](docs/observation.md)，无需 Case、Judge 或自动上传。
- [运行全栈示例](examples/full_stack/README.zh-CN.md)，在 Docker 中组合 KUMA
  与 mini-SWE-agent；该流程可能消耗服务 Credit 和模型预算。

## 文档

[SDK 指南](docs/sdk-guide.md) · [Python API 参考](docs/api-reference.md) ·
[版本与发布](docs/releases.md) · [详细 Judge 评估](docs/judge-assessment.md)

## 项目链接

[安全策略](SECURITY.md) · [贡献说明](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
