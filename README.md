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

## Core capabilities

- Synchronous, framework-neutral Case and Judge workflow.
- Official or custom Providers, including fully local runs.
- Bounded file, log, and optional trace Evidence. Runtime Evidence v1 remains hash-only; when the official service explicitly advertises v2, the completed step may include the final Agent output after strict JSON, size, and sensitive-data checks.

## Detailed documentation

[English SDK guide](docs/sdk-guide.md) · [Python API reference](docs/api-reference.md) · [简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [Runtime Evidence contract](docs/runtime-evidence.md)

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
