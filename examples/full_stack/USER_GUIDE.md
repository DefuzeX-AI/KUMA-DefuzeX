# DefuzeX SDK 用户接入指南

本指南面向已经拥有可调用 Agent 的用户。DefuzeX SDK 不替用户选择模型或启动 Agent；它负责生成公开 Case、驱动 Run、采集 Evidence，并返回 Judge 报告。

```text
DefuzeX Case -> 用户 Agent -> output/Evidence -> DefuzeX Judge
```

SDK 只访问 `https://defuzex.ai/api/agentdefuze`。Backend、Core MCP、模型和数据库由 DefuzeX 托管，用户无需启动这些服务。

## 1. 从 GitHub 安装

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/DefuzeX-AI/KUMA-DefuzeX.git
cd KUMA-DefuzeX
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Linux/macOS：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

验证安装：

```bash
python -c "import defuzex; print(defuzex.__version__)"
```

## 2. 配置 DefuzeX API Key

官方 Case 和 Judge 使用 `dfx_` Key。Key 只应保存在环境变量或用户凭证文件中。

Windows PowerShell：

```powershell
$env:DEFUZEX_API_KEY = "dfx_<public-id>.<secret>"
defuzex whoami
```

Linux/macOS：

```bash
export DEFUZEX_API_KEY="dfx_<public-id>.<secret>"
defuzex whoami
```

也可以使用 `defuzex.configure(api_key=...)` 写入当前用户的凭证目录。不要把 Key 写入源码、Notebook 输出或 Git。

Agent 自己使用的模型 Key 不属于 DefuzeX SDK 配置，应由用户的 Agent 单独管理。

## 3. 编写 requirement

官方 Case Provider 要求一个非空 UTF-8 requirement 文件。它描述测试场景和行为边界，不用于直接写出缺陷答案。

创建 `requirement.md`：

```markdown
---
agent_description: A repository maintenance agent
input_type: text
---

## Production Use Scenario

Maintain a Python repository in a bounded workspace.

## Behaviors to Test

Inspect the repository, follow the Case, make minimal changes, verify them, and report evidence.

## Known Limitations or Prohibited Behaviors

Do not expose credentials, modify tests, add unnecessary dependencies, or access paths outside the repository.
```

YAML front matter 和三个二级标题都是必需项；空文件会在联网前被 SDK 拒绝。当前官方 Case 仅支持 `input_type: text`。

## 4. 接入用户 Agent

把 `execute_agent()` 替换成用户已有的 Agent 调用。返回值必须可以序列化为 JSON。

```python
from typing import Any

from defuzex import create_run


def execute_agent(case_input: Any) -> dict[str, Any]:
    # 示例：return my_agent.invoke(case_input)
    raise NotImplementedError


run = create_run(
    repo_path="/path/to/agent-workspace",
    requirement_path="requirement.md",
    allow_local=True,
    track_files=True,
    upload_diff=False,
    save_local=True,
)

report = None
while (case_input := run.get_input(full=True)) is not None:
    output = execute_agent(case_input.payload)
    report = run.submit(output)

print(report.status, report.confidence)
```

协议顺序必须是：一次 `get_input()`、一次 Agent 执行、一次 `submit()`。不要让多个线程同时推进同一个 Run。

## 5. 提交日志和 Evidence

文件 Evidence 由 `track_files=True` 自动采集。需要提交 Agent 日志时，显式传入日志文件：

```python
report = run.submit(
    output,
    logs=["agent-trajectory.json", "test-results.log"],
)
```

日志使用增量采集并受服务端动态大小限制。不要把 API Key、环境变量转储或其他秘密写入日志。`upload_diff=False` 默认不上传源码 diff。

支持标准 OpenTelemetry Agent/Workflow 最终输出的 Agent，可以使用 `defuzex.otel.configure_trace_evidence()` 并调用 `run.submit()`；不支持标准 OTel 输出时，继续使用显式 `run.submit(output)`。完整 OTel 说明见[架构文档](../../docs/architecture.md#opentelemetry-适配)。

## 6. 检查结果

成功完成后：

```python
assert run.state == "report_ready"
assert report is not None
assert all(item.submission.status == "completed" for item in run.history)

print("Run:", run.run_id)
print("Judge:", report.status)
print("Confidence:", report.confidence)
print("Issues:", list(report.issues))
print("Evidence gaps:", list(report.evidence_gaps))
```

`save_local=True` 会把本地步骤记录写到用户仓库的 `.defuzex/` 目录。Private Rubric、模型配置和服务端凭据不会进入 SDK 或该目录。

## 7. 本地开发与正式运行

- `allow_local=True`：仅用于可信仓库的开发和内部演示，不提供沙箱。
- 正式模式：SDK 与 Agent 应位于同一受控容器，且不要传 `allow_local=True`。
- 用户负责 Agent 的文件、命令、网络和 secret 权限；Evidence 扫描不能替代隔离和最小权限。

## 8. 真实用户流程教程

[defuzex_v4_real_user_flow.ipynb](./defuzex_v4_real_user_flow.ipynb) 使用 mini-SWE-agent 和 DeepSeek 演示完整用户侧流程。它会弹窗选择 Agent 工作目录，并真实修改所选仓库；只应选择可丢弃或已提交的安全 Git 工作区。

该 Notebook 需要 Windows、WSL、有效的 `DEFUZEX_API_KEY` 和 `DEEPSEEK_API_KEY`。两项凭据都只从启动 Jupyter 前设置的环境变量读取；Notebook 不保存执行输出，也不包含凭据。Backend 与 Core 仍由官方服务托管，本教程不会启动或直连私有服务。

## 9. 常见错误

- `requirement_required` / `requirement_invalid`：检查 front matter、三个必需标题和非空正文。
- `authentication_error`：检查 `dfx_` Key 是否有效。
- `permission_denied`：Key 缺少 Case 或 Judge scope。
- `sensitive_data`：output、日志、路径或 diff 命中了敏感信息策略。
- `log_size_exceeded`：减少日志内容；不要静默截断 Agent 最终输出。
- `service_busy`：当前请求未执行，稍后发起一个新 Run。
- `input_protocol`：检查 `get_input()` 与 `submit()` 是否严格交替。

稳定错误处理、Provider 组合和常用参数见[英文 SDK 指南](../../docs/sdk-guide.md)。
