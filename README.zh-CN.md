<p align="center">
  <img src="docs/assets/defuzex-banner.svg" width="760" alt="KUMA geometric wordmark banner">
</p>

<h1 align="center">KUMA</h1>

<p align="center">
  <strong>DefuzeX Python SDK</strong><br>
  面向 Agent 的知识与证据驱动通用评测
</p>

<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10--3.14-3776AB?logo=python&amp;logoColor=white" alt="支持 Python 3.10 至 3.14">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache--2.0-4C8CBF" alt="Apache 2.0 许可证"></a>
</p>

DefuzeX 是公开 Python SDK，用于通过本地或官方 Case、Judge Provider 对 Agent 行为进行有边界 Evidence 的测试。

## 为什么使用 DefuzeX

- **严格且不绑定框架的协议：**通过同步 `get_input()` → Agent → `submit()` 生命周期接入现有 Agent。
- **边界明确的有效 Evidence：**按有界、隐私友好的默认策略采集文件元数据、指定日志和可选的同进程 OpenTelemetry spans。
- **本地起步，按需接入服务：**无需账号或网络即可首次运行，需要时再通过公开 HTTPS API 使用官方 Case 与 Judge。

## 安装

需要 Python 3.10 或更高版本。安装当前源码：

```bash
git clone https://github.com/DefuzeX-AI/KUMA-DefuzeX.git
cd KUMA-DefuzeX
python -m pip install .
```

虚拟环境、可选 OpenTelemetry 能力和贡献者环境见[中文接入指南](examples/full_stack/USER_GUIDE.md#1-从-github-安装)。

## 最小可运行示例

运行确定性的本地检查；无需账号、API Key、Docker 或网络：

```bash
defuzex quickstart
```

预期结果：

```text
Local check: PASS
Score: 100/100
Reason: Output exactly matched the published rule.
Artifact: <temporary directory>/result.json
```

如需运行完整的本地 `Run`，执行 [`examples/minimal_local.py`](examples/minimal_local.py)：

```bash
python examples/minimal_local.py
```

## 核心能力

- 同步、单 Case 的 [`Run` 生命周期](docs/architecture.md#run-状态机)，包含不可变的 Input、Submission、History 和 Report。
- 支持官方或自定义 [Case 与 Judge Provider](docs/architecture.md#provider)，也可完全本地运行。
- 有界 [Evidence 采集](docs/architecture.md#trackingevidence-与隐私)，并提供规范、仅含哈希的 [Runtime Evidence 合同](docs/runtime-evidence.md)。
- 可选的 [OpenTelemetry Trace Evidence](docs/architecture.md#opentelemetry-适配)，不替换应用已有的 Tracer Provider。
- 仅访问公开 HTTPS 服务；SDK 不托管 Agent、模型、数据库或私有评估资产。参见[架构](docs/architecture.md)和[公开 API Contract](docs/api-contract.md)。

## 支持与项目链接

- [中文接入指南](examples/full_stack/USER_GUIDE.md) · [Single Agent 模板](examples/single_agent_template/README.md) · [英文 SDK 指南](docs/sdk-guide.md)
- [GitHub Issues](https://github.com/DefuzeX-AI/KUMA-DefuzeX/issues) · [安全策略](SECURITY.md)
- [贡献说明](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
