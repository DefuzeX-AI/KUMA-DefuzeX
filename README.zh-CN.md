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

- 通过统一、框架无关的 Python 工作流，完成从 Case 到 Judgment 的可重复评测。
- 可使用 KUMA 官方服务，也可接入自定义 Case 和 Judge Provider，或保持全本地运行。
- 有界采集步骤结果、文件变更、显式日志与可选 OpenTelemetry Trace，
  形成结构化 Evidence。
- 通过带版本的 Strategy Group 选择测试方法，并用 Agent Profile 描述被测
  Agent 及其运行上下文。

## 安装

从 PyPI 安装，需要 Python 3.10 或更高版本，无需 Git：

```bash
python -m pip install --upgrade kuma-defuzex
```

请在 Agent 实际使用的 Python 环境中运行，安装后重启 Agent。
安装包名为 `kuma-defuzex`，Python 导入和命令行名称仍为 `kuma`。
可选 OTel 依赖与 Trace 配置见 [Runtime Trace 指南](docs/runtime-trace.zh-CN.md)。

## 版本与更新提醒

本文档对应 **KUMA SDK 0.3.0**。运行 `python -c "import kuma; print(kuma.__version__)"`
确认安装版本；下述观察与关联 API 需要 0.3.0 或更新版本。
是否可下载安装以 [PyPI](https://pypi.org/project/kuma-defuzex/) 和对应的
[官方 Release](https://github.com/DefuzeX-AI/KUMA-DefuzeX/releases) 为准，源码版本本身不代表已发布。
运行 `kuma updates check`（或 Python `kuma.check_for_updates()`）检查正式版：
只升补丁号为**可选更新**，主/次版本提高为**必须升级提醒**，但不阻断任务、不自动安装。
官方请求后台检查，import/help/local/custom 不联网检查；设置
`KUMA_DISABLE_UPDATE_CHECK=1` 可全部禁用。离线失败不影响业务，结果按进程缓存
24 小时。0.1.0 用户需先手动升级一次才能获得提醒。[完整规范与限制](docs/releases.md)。

## 快速开始

无需账号、API Key、Docker 或网络即可运行确定性的本地检查：

```bash
kuma quickstart
```

## 本地观察

[`observe()`](docs/observation.md) 可观察已有 instrumentation 的 Agent，
无需账号、Case 或 Judge，不会自动上传。已识别的模型/工具内容采用有界、
脱敏投影，不是放开任意属性。源码仓库提供[离线框架示例](docs/instrumentation-examples.md)。
[云端保存](docs/cloud-observations.md) 必须显式认证调用；
[Run 关联](docs/run-correlation.md) 区分执行、采集、评测和可测阶段耗时。
本地观察不需要 KUMA 服务。云端保存与官方 Run 关联要求服务端实现对应公开合同，
并启用所需权限和能力；安装 SDK 不会自动开通服务端功能，不可用时会明确报错。

## 全栈用户流程示例

按照[全栈用户流程指南](examples/full_stack/README.zh-CN.md)，可在 Docker 中组合运行 KUMA 与 mini-SWE-agent。该流程会调用外部服务，可能消耗服务 Credit 和模型预算。

## 详细文档

[保存并复用 Case](docs/case-files.zh-CN.md)

[Runtime Trace 与可选文件 diff](docs/runtime-trace.zh-CN.md)

详细指南现统一使用英文：[SDK 指南](docs/sdk-guide.md) · [策略组](docs/strategy-groups.md) · [Agent 工具能力](docs/agent-tool-capabilities.md) · [API 参考](docs/api-reference.md) · [Agent Profile 迁移说明](docs/migration-agent-profile.md) · [Runtime Evidence 合同](docs/runtime-evidence.md)

## 项目链接

[安全策略](SECURITY.md) · [贡献说明](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
