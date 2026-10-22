# 为 KUMA Python SDK 贡献代码

感谢你帮助改进这个开源 SDK。请将改动限制在客户端的职责范围内：
公共协议模型、传输行为、Run 生命周期、Evidence 构建，以及 Provider 接口。
后端、私有 Core MCP、模型执行、凭证、提示词和数据库不属于本仓库范畴。

## 开发环境搭建

使用 Python 3.10 或更高版本，在隔离环境中开发：

```bash
python -m venv .venv
```

安装依赖前先激活虚拟环境。

**Windows PowerShell：**

```powershell
.venv\Scripts\Activate.ps1
```

**macOS/Linux：**

```bash
source .venv/bin/activate
```

确认虚拟环境的 Python 解释器已激活：

```bash
python -c "import sys; print(sys.executable)"
```

然后安装开发依赖：

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev,otel]"
```

运行标准检查项：

```bash
python -m ruff format --check --exclude "*.ipynb" .
python -m ruff check --exclude "*.ipynb" .
python tools/verify_public_api_docs.py
python tools/verify_agent_profile_migration.py
python tools/verify_evidence_capacity.py
python tools/verify_executed_strategy_group.py
python tools/verify_cli_atomic_output.py
python tools/verify_transport_failures.py
python tools/verify_judge_busy.py
python tools/verify_operation_poll_backoff.py
python tools/verify_privacy_authorization.py
python tools/verify_safe_diagnostics.py
python tools/verify_otel_compat.py
python -m compileall -q src examples tools
kuma quickstart
python examples/minimal_local.py
python tools/verify_minimal_local.py
python -m build
python -m twine check dist/*
```

`tools/verify_otel_compat.py` 需要安装上面的 `otel` 扩展依赖；
CI 也会针对多个固定版本的 OpenTelemetry 运行该检查。

公共 CI 会检查代码规范、受支持 Python 版本的安装与导入、CLI、
离线示例，以及包构建。本公共仓库不包含维护者完整的安全和契约回归测试套件。
维护者在接受发布前会运行这些私有检查。

执行组回归检查使用合成响应和临时仓库，无需账号或网络，
覆盖 Run 元数据、无需再次提交 Case 的无效响应恢复，以及已保存 Case 的复用。

CLI 清理检查使用临时文件。
传输中断检查仅使用合成凭证和本地回环 HTTP 服务器；
不会调用任何托管 API、模型或付费评测服务。

授权隐私检查也是离线的。它覆盖原始文档/curl 示例、
格式错误的边界、相邻凭证和脱敏后的重新扫描。

安全诊断检查仅使用合成的公共 payload 和真实的 SDK 验证器。
它覆盖本地报告/步骤解释、可选的封闭 HTTP 准入错误、
服务生成的敏感 Case 错误，以及完整的大括号环境引用边界。
它在本仓库中离线运行，不需要私有测试、凭证、模型调用或额外的测试框架。

## Pull Request 规范

- 使用 fork 或专注的分支；不要直接向 `main` 提交代码。
- 每个 PR 只包含一个已确认的行为变更、缺陷修复或文档更新。
- 保持公共契约不变，并描述行为变更的可复现验证方法。
- 不要削弱验证逻辑、隐藏错误，或包含生成的构建产物。
- 绝对不要提交 API Key、`.env` 文件、本地路径、私有 Rubric、
  模型提示词、服务地址、数据库或用户数据。
- 在 PR 中说明兼容性、安全性和隐私方面的影响。
- 保持公共 API 文档与函数签名和行为同步。
  每个参数都必须说明其类型、是否必填/默认值、单位或取值范围、
  哨兵值的含义，以及与安全相关的影响。
  如果返回值、稳定异常、状态/资源副作用、重试机制等细节不够直观，
  也需要文档化说明，并附上一个经过验证的最小示例。
  运行 `python tools/verify_public_api_docs.py`；
  它能检测签名/表格不一致，但不能替代人工语义审查。

提交贡献即表示你同意该贡献依据 Apache License 2.0 许可发布。
安全问题请按照 [SECURITY.md](SECURITY.md) 中的说明私下报告，
不要通过公开 Issue 提交。
