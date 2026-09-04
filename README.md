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

## Core capabilities

- Synchronous, framework-neutral Case and Judge workflow.
- Official or custom Providers, including fully local runs.
- Bounded file, log, and optional trace Evidence.
- Strategy Groups choose the testing method; an Agent Profile supplies only the
  Agent, scenario, expected-behavior, and prohibited-boundary context.

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

## Full-stack user-flow example

Follow the [full-stack user-flow guide](examples/full_stack/README.md) to run KUMA with mini-SWE-agent in Docker. This path calls external services and may use service credit and model budget.

## Detailed documentation

[English SDK guide](docs/sdk-guide.md) · [Strategy Groups](docs/strategy-groups.md) · [Agent tool capabilities](docs/agent-tool-capabilities.md) · [Python API reference](docs/api-reference.md) · [Agent Profile migration](docs/migration-agent-profile.md) · [简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [Runtime Evidence contract](docs/runtime-evidence.md)

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
