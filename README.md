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

## Choose a Strategy Group

For official runs, configure `KUMA_API_KEY`, then fetch the current public catalog:

```bash
kuma strategies list
kuma strategies list --output strategy-groups.json
```

KUMA validates the catalog before showing or saving its group `id`/`version`, `display_name`, `description`, `available`, `required_capabilities`, `limits.max_steps`, `limits.supported_difficulties`, and exact `default` coordinate. Copy a selected coordinate into the Requirement front matter:

```yaml
strategy_group:
  schema_version: kuma.strategy_group_selection.v1
  id: <catalog group id>
  version: "<catalog version>"
```

Only these three fields are user-owned; KUMA fills `selection_source` and `catalog_release`. An unknown, unavailable, or capability-incompatible explicit selection fails closed. Omitting `strategy_group` uses the catalog's `default.id` and `default.version`; “general” is its meaning, not a fixed ID. `scan_strategy_group=True` is an explicit opt-in to conservative local suggestion. See [Strategy Groups](docs/strategy-groups.md).

## Full-stack user-flow example

Follow the [full-stack user-flow guide](examples/full_stack/README.md) to run KUMA with mini-SWE-agent in Docker. This path calls external services and may use service credit and model budget.

## Detailed documentation

[English SDK guide](docs/sdk-guide.md) · [Python API reference](docs/api-reference.md) · [简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [Runtime Evidence contract](docs/runtime-evidence.md)

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
