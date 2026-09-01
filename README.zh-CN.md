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

## 真实全流程示例

仓库提供了一个可直接运行的 [Docker 用户流程](examples/full_stack/docker_user_flow.py)，覆盖完整的官方调用链：

```text
KUMA SDK → 公网 Backend → Core 评测服务 → 公共 Judgment
```

该示例会获取官方 Case，把每个 Case 步骤交给 mini-SWE-agent，提交有界的文件、日志和 OTel Evidence，取得官方 Judgment，并将其中的公共字段保存到 `.kuma/mini-swe-agent/judge-report.json`。

示例的核心 Run 循环如下：

```python
run = create_run(
    repo_path=REPO,
    requirement_path=REQUIREMENT,
    track_files=True,
    save_local=True,
    trace_evidence=trace_evidence,
)

report = None
while (case_input := run.get_input(full=True)) is not None:
    result = run_mini_swe_agent(str(case_input.payload), step_index)
    log_keys = {"evidence_log", "test_log", "trajectory_log"}
    output = {key: value for key, value in result.items() if key not in log_keys}
    report = run.submit(output, logs=[result["evidence_log"]])
    step_index += 1
```

链接的源码包含真实运行使用的 Agent adapter、有界执行、验证和报告持久化逻辑。按照[全流程指南](examples/full_stack/USER_GUIDE.md)准备一次性工作区，设置 `KUMA_BASE_URL`、`KUMA_API_KEY` 和 `DEEPSEEK_API_KEY`，然后执行：

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t kuma-user-flow .
workspace=/absolute/path/to/prepared-workspace
docker run --rm \
  --env KUMA_BASE_URL \
  --env KUMA_API_KEY \
  --env DEEPSEEK_API_KEY \
  --mount "type=bind,source=$workspace,target=/workspace" \
  kuma-user-flow
```

该流程会调用真实服务，可能消耗服务 Credit 和模型预算。KUMA 只请求用户配置的公网 Backend，不要求用户提供私有 Core 地址或凭据。

## 核心能力

- 同步且不绑定框架的 Case 与 Judge 流程。
- 支持官方或自定义 Provider，也可完全本地运行。
- 有界采集文件、日志和可选 Trace Evidence，并提供规范、仅含哈希的 Runtime Evidence 合同。

## 详细文档

[简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [English SDK guide](docs/sdk-guide.md) · [Python API reference](docs/api-reference.md) · [Runtime Evidence 合同](docs/runtime-evidence.md)

## 项目链接

[安全策略](SECURITY.md) · [贡献说明](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
