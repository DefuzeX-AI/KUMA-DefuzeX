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

The runnable [Docker example](examples/full_stack/docker_user_flow.py) performs the real flow: obtain an official Case, run each step with mini-SWE-agent, submit Evidence, receive the official Judgment, and save it as `.kuma/mini-swe-agent/judge-report.json`.

Prepare a disposable Agent workspace using the [short guide](examples/full_stack/USER_GUIDE.md), set `KUMA_BASE_URL`, `KUMA_API_KEY`, and `DEEPSEEK_API_KEY`, then run from this repository:

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

This calls real services and may use service credit and model budget. KUMA needs only the public Backend URL and user keys—never a private Core address.

## Core capabilities

- Synchronous, framework-neutral Case and Judge workflow.
- Official or custom Providers, including fully local runs.
- Bounded file, log, and optional trace Evidence, plus a canonical hash-only Runtime Evidence contract.

## Detailed documentation

[English SDK guide](docs/sdk-guide.md) · [Python API reference](docs/api-reference.md) · [简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [Runtime Evidence contract](docs/runtime-evidence.md)

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
