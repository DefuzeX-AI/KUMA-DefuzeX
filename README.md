<p align="center">
  <img src="docs/assets/kuma-banner.svg" width="760" alt="KUMA geometric wordmark banner">
</p>

<h1 align="center">KUMA</h1>

<p align="center">
  <strong>KUMA Python SDK</strong><br>
  Knowledge-grounded Universal Measurement for Agents
</p>

<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README.zh-CN.md">简体中文</a>
</p>

KUMA is the public Python SDK for testing Agent behavior through a strict `Run` protocol and bounded Evidence capture. Official services are reached only through public HTTPS; the SDK does not run Agents, execute models, or expose private evaluation logic.

## Install

Python 3.10 or newer is required:

```bash
python -m pip install "kuma-defuzex==0.1.0"
```

## Quick start

Run the deterministic local check without an account, API key, Docker, or network:

```bash
kuma quickstart
```

## Real end-to-end example

The repository includes a runnable [Docker user flow](examples/full_stack/docker_user_flow.py) that exercises the complete official path:

```text
KUMA SDK → public Backend → Core evaluation service → public Judgment
```

The example requests an official Case, passes every Case step to mini-SWE-agent, submits bounded file/log/OTel Evidence, obtains the official Judgment, and writes its public fields to `.kuma/mini-swe-agent/judge-report.json`.

Its central Run loop is:

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

The linked source contains the Agent adapter, bounded execution, verification, and report persistence used by the actual run. To execute it, prepare a disposable workspace as described in the [full-stack guide](examples/full_stack/USER_GUIDE.md), set `KUMA_BASE_URL`, `KUMA_API_KEY`, and `DEEPSEEK_API_KEY`, then run:

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

This flow calls real services and may consume service credit and model budget. KUMA sends requests only to the configured public Backend; it never asks the user for a private Core address or credential.

## Core capabilities

- Synchronous, framework-neutral Case and Judge workflow.
- Official or custom Providers, including fully local runs.
- Bounded file, log, and optional trace Evidence, plus a canonical hash-only Runtime Evidence contract.

## Detailed documentation

[English SDK guide](docs/sdk-guide.md) · [Python API reference](docs/api-reference.md) · [简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [Runtime Evidence contract](docs/runtime-evidence.md)

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
