# KUMA 全栈用户流程示例

[English](README.md) | [简体中文](README.zh-CN.md)

本指南只说明仓库提供的 mini-SWE-agent 示例。SDK 的通用安装、API Key 配置、Agent Profile 格式、Run 协议、Evidence、OpenTelemetry、Docker 边界和故障排查，请查阅规范的[简体中文指南](../../docs/sdk-guide.zh-CN.md)或[英文指南](../../docs/sdk-guide.md)。

## 示例内容

该示例在同一个 Docker 容器中组合 KUMA SDK 与 mini-SWE-agent。它会请求官方 Case 与 Judge，在挂载的工作区内执行每个 Case 步骤，记录有界 Trace 和日志 Evidence，并在本地写入公开 Judge 结果。

示例提供两个入口：

- 使用 [`Dockerfile.user-flow`](Dockerfile.user-flow) 和 [`docker_user_flow.py`](docker_user_flow.py) 直接运行容器。
- 使用 [`kuma_real_user_flow.ipynb`](kuma_real_user_flow.ipynb)完成 Windows/WSL 引导流程。

两条路径都会调用真实外部服务，可能产生模型或服务费用。

## 准备工作区

请使用一次性 Git 工作区，或确保其中所有既有改动均已提交。直接运行 Docker 示例时，挂载目录必须包含：

- 符合支持格式的 `agent-profile.md`；
- `calculator.py`，这是示例唯一允许修改的源码文件；
- 可通过 `python -m unittest discover -v` 运行的测试。

运行前设置以下环境变量：

- `KUMA_BASE_URL`
- `KUMA_API_KEY`
- `DEEPSEEK_API_KEY`

不要将真实凭证写入工作区或仓库。
只配置公开的 KUMA Backend URL；本示例不需要私有 Core 地址。

## 构建镜像

在 SDK 仓库根目录执行：

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t kuma-user-flow .
```

## 运行容器

Windows PowerShell：

```powershell
$workspace = (Resolve-Path "C:\path\to\workspace").Path
docker run --rm `
  --env KUMA_BASE_URL `
  --env KUMA_API_KEY `
  --env DEEPSEEK_API_KEY `
  --mount "type=bind,source=$workspace,target=/workspace" `
  kuma-user-flow
```

Linux 或 macOS：

```bash
workspace="$(pwd)"
docker run --rm \
  --env KUMA_BASE_URL \
  --env KUMA_API_KEY \
  --env DEEPSEEK_API_KEY \
  --mount "type=bind,source=$workspace,target=/workspace" \
  kuma-user-flow
```

脚本会拒绝在 Docker 外运行，也会拒绝缺失的环境变量。如果 Agent 修改了范围外源码、Submission 未成功或最终 Judge 报告缺失，运行同样会失败。

## 运行 Notebook

Notebook 需要 Windows、WSL、Jupyter、上述两个 API Key 和已配置的公开 Base URL。启动 Jupyter 前先设置环境变量，再打开 [`kuma_real_user_flow.ipynb`](kuma_real_user_flow.ipynb)，按照单元格提示选择 Agent 工作区。

Notebook 可能修改所选仓库。请仅选择一次性工作区，或确保仓库原有工作均已提交。

## 输出

直接运行流程会在挂载工作区的 `.kuma/mini-swe-agent/` 下写入：

- 精简的 Agent trajectory 与验证 Evidence；
- 各步骤的 unittest 日志；
- 最终公开 `judge-report.json`。

脚本还会输出官方 Case Inputs、最终报告、采集的 span 名称、最终 `calculator.py` 和 artifact 路径。这些输出只属于本示例；通用结果处理方式以规范 SDK 指南为准。
