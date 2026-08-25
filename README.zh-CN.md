<p align="center">
  <img src="docs/assets/defuzex-banner.svg" width="760" alt="KUMA geometric wordmark banner">
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

## 安装

需要 Python 3.10 或更高版本：

```bash
git clone https://github.com/DefuzeX-AI/KUMA-DefuzeX.git
cd KUMA-DefuzeX
python -m pip install .
```

## 快速开始

无需账号、API Key、Docker 或网络即可运行确定性的本地检查：

```bash
defuzex quickstart
```

## 核心能力

- 同步且不绑定框架的 Case 与 Judge 流程。
- 支持官方或自定义 Provider，也可完全本地运行。
- 有界采集文件、日志和可选 Trace Evidence，并提供规范、仅含哈希的 Runtime Evidence 合同。

## 详细文档

[简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [English SDK guide](docs/sdk-guide.md) · [Runtime Evidence 合同](docs/runtime-evidence.md)

## 项目链接

[安全策略](SECURITY.md) · [贡献说明](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
