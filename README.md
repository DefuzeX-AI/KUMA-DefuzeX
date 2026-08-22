<p align="center">
  <img src="docs/assets/defuzex-banner.svg" width="760" alt="KUMA geometric wordmark banner">
</p>

<h1 align="center">KUMA</h1>

<p align="center">
  <strong>DefuzeX Python SDK</strong><br>
  Knowledge-grounded Universal Measurement for Agents
</p>

<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10--3.14-3776AB?logo=python&amp;logoColor=white" alt="Python 3.10 to 3.14">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache--2.0-4C8CBF" alt="Apache 2.0 license"></a>
</p>

DefuzeX is the public Python SDK for running evidence-bounded Agent behavior tests with local or official Case and Judge providers.

## Why DefuzeX

- **A strict, framework-neutral protocol:** connect an existing Agent through the synchronous `get_input()` → Agent → `submit()` lifecycle.
- **Useful evidence with explicit boundaries:** capture file metadata, selected logs, and optional in-process OpenTelemetry spans with bounded, privacy-aware defaults.
- **Local first, hosted when needed:** start without an account or network, then use official Case and Judge services through the public HTTPS API.

## Install

Python 3.10 or newer is required. Install the current source checkout:

```bash
git clone https://github.com/DefuzeX-AI/KUMA-DefuzeX.git
cd KUMA-DefuzeX
python -m pip install .
```

For virtual environments, optional OpenTelemetry support, and contributor setup, see the [SDK guide](docs/sdk-guide.md#installation).

## Minimal runnable example

Run the deterministic local check—no account, API key, Docker, or network required:

```bash
defuzex quickstart
```

Expected result:

```text
Local check: PASS
Score: 100/100
Reason: Output exactly matched the published rule.
Artifact: <temporary directory>/result.json
```

For a complete local `Run`, execute [`examples/minimal_local.py`](examples/minimal_local.py):

```bash
python examples/minimal_local.py
```

## Core capabilities

- Synchronous, single-Case [`Run` lifecycle](docs/sdk-guide.md#run-lifecycle-and-providers) with immutable inputs, submissions, history, and reports.
- Official or custom [Case and Judge providers](docs/sdk-guide.md#run-lifecycle-and-providers), including fully local operation.
- Bounded [Evidence capture](docs/sdk-guide.md#evidence-and-opentelemetry) for file changes, explicit logs, and capture gaps.
- Optional [OpenTelemetry trace evidence](docs/sdk-guide.md#evidence-and-opentelemetry) without replacing the application's tracer provider.
- Public HTTPS-only service boundary; the SDK does not host Agents, models, databases, or private evaluation assets. See [architecture](docs/architecture.md) and the [public API contract](docs/api-contract.md).

## Support and project links

- [SDK guide](docs/sdk-guide.md) · [Single Agent template](examples/single_agent_template/README.md) · [Docker user flow](examples/full_stack/USER_GUIDE.md)
- [GitHub Issues](https://github.com/DefuzeX-AI/KUMA-DefuzeX/issues) · [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
