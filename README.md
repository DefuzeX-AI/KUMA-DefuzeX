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
- `upload_diff=True` with `track_files=True` sends safe patches; default is
  hash-only. Trace attaches to configured OTel and uploads only recorded tool
  content, never invented arguments/results. [Setup and source upgrade](docs/runtime-trace.md).
- Default Trace budget: 8 MiB per Run. Agent output: 4 MiB canonical JSON;
  Runtime Evidence: 5 MiB; complete multipart upload: 8 MiB. Stricter server
  limits still apply; output is rejected rather than truncated.
- Strategy Groups choose the testing method; an Agent Profile supplies only the
  Agent, scenario, expected-behavior, and prohibited-boundary context.

## Install

Install the latest GitHub `main` source. Requires Python 3.10 or newer and Git:

```bash
python -m pip install --upgrade "git+https://github.com/DefuzeX-AI/KUMA-DefuzeX.git"
```

Run this in the Python environment used by your Agent, then restart the Agent.
The package is not currently available on PyPI; install from GitHub as above.
For optional OTel dependencies and Trace setup, see the
[Runtime Trace guide](docs/runtime-trace.md).

## Versions and update reminders

SDK version: **0.2.2**. Publication status is shown in the
[official Releases](https://github.com/DefuzeX-AI/KUMA-DefuzeX/releases).
Run `kuma updates check` (or Python `kuma.check_for_updates()`) to check stable
Release tags: patch-only updates are **optional**, higher major/minor versions
**require an upgrade reminder**, but never block work or install automatically.
Official requests check in the background; import/help/local/custom do not.
Set `KUMA_DISABLE_UPDATE_CHECK=1` to disable all checks. Offline failures are safe;
results are cached for 24 hours per process. Old 0.1.0 users must upgrade manually
once to gain reminders. [Full policy and limits](docs/releases.md).

## Quick start

Run the deterministic local check without an account, API key, Docker, or network:

```bash
kuma quickstart
```

## Full-stack user-flow example

Follow the [full-stack user-flow guide](examples/full_stack/README.md) to run KUMA with mini-SWE-agent in Docker. This path calls external services and may use service credit and model budget.

## Detailed documentation

[Save and reuse a Case](docs/case-files.md)

[Runtime Trace and optional file diffs](docs/runtime-trace.md)

[English SDK guide](docs/sdk-guide.md) · [Strategy Groups](docs/strategy-groups.md) · [Agent tool capabilities](docs/agent-tool-capabilities.md) · [Python API reference](docs/api-reference.md) · [Agent Profile migration](docs/migration-agent-profile.md) · [简体中文 SDK 指南](docs/sdk-guide.zh-CN.md) · [中文 API 参考](docs/api-reference.zh-CN.md) · [Runtime Evidence contract](docs/runtime-evidence.md)

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
