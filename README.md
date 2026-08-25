<p align="center">
  <img src="docs/assets/defuzex-banner.svg" width="760" alt="KUMA geometric wordmark banner">
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
git clone https://github.com/DefuzeX-AI/KUMA-DefuzeX.git
cd KUMA-DefuzeX
python -m pip install .
```

## Quick start

Run the deterministic local check without an account, API key, Docker, or network:

```bash
defuzex quickstart
```

## Core capabilities

- Synchronous, framework-neutral Case and Judge workflow.
- Official or custom Providers, including fully local runs.
- Bounded file, log, and optional trace Evidence, plus a canonical hash-only Runtime Evidence contract.

## Detailed documentation

[English SDK guide](docs/sdk-guide.md) · [简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [Runtime Evidence contract](docs/runtime-evidence.md)

Docker builds retain local images and cache; review [local Docker storage](docs/sdk-guide.md#local-docker-storage) periodically.

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
