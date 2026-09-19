<p align="center">
  <img src="docs/assets/kuma-banner.svg" width="760" alt="KUMA geometric wordmark banner">
</p>

<h1 align="center">KUMA</h1>

<p align="center">
  <strong>KUMA Python SDK</strong><br>
  Knowledge-grounded Universal Measurement for Agents
</p>

<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README.zh-CN.md">Chinese overview</a>
</p>

KUMA is the public Python SDK for testing Agent behavior through a strict `Run` protocol and bounded Evidence capture. Official services are reached only through public HTTPS; the SDK does not run Agents, execute models, or expose private evaluation logic.

## Core capabilities

- Run repeatable Case-to-Judgment evaluations through one framework-neutral
  Python workflow.
- Use KUMA's official services, plug in custom Case and Judge Providers, or keep
  the workflow fully local.
- Capture bounded, structured Evidence from step results, file changes, explicit
  logs, and optional OpenTelemetry traces.
- Select the testing method with a versioned Strategy Group, and describe the
  Agent and its operating context with an Agent Profile.

## Install

Install from PyPI. Requires Python 3.10 or newer; Git is not required:

```bash
python -m pip install --upgrade kuma-defuzex
```

Run this in the Python environment used by your Agent, then restart the Agent.
The package name is `kuma-defuzex`; Python imports and the CLI remain `kuma`.
For optional OTel dependencies and Trace setup, see the
[Runtime Trace guide](docs/runtime-trace.md).

## Versions and update reminders

This documentation describes **KUMA SDK 0.3.0**. Confirm the installed version
with `python -c "import kuma; print(kuma.__version__)"`. The observation and
correlation APIs below require 0.3.0 or newer. Package availability is determined
by [PyPI](https://pypi.org/project/kuma-defuzex/) and the matching
[official Release](https://github.com/DefuzeX-AI/KUMA-DefuzeX/releases), not a
source checkout alone.
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

## Local observation

Use [local observation](docs/observation.md) to inspect an already instrumented
Agent without an account, Case, Judge or automatic upload. Try the
[offline framework examples](docs/instrumentation-examples.md) from this source
checkout. [Cloud storage](docs/cloud-observations.md) requires a separate explicit
authenticated call; [Run correlation](docs/run-correlation.md) distinguishes
execution, capture and evaluation instead of treating them as the same result.

Local observation needs no KUMA service. Cloud storage and official Run
correlation require a service implementing the corresponding public contracts,
with the required permissions and capabilities enabled. Installing the SDK does
not activate server features; unavailable service features fail explicitly.

## Full-stack user-flow example

Follow the [full-stack user-flow guide](examples/full_stack/README.md) to run KUMA with mini-SWE-agent in Docker. This path calls external services and may use service credit and model budget.

## Detailed documentation

[Save and reuse a Case](docs/case-files.md)

[Runtime Trace and optional file diffs](docs/runtime-trace.md)

[SDK guide](docs/sdk-guide.md) · [Strategy Groups](docs/strategy-groups.md) · [Agent tool capabilities](docs/agent-tool-capabilities.md) · [Python API reference](docs/api-reference.md) · [Agent Profile migration](docs/migration-agent-profile.md) · [Runtime Evidence contract](docs/runtime-evidence.md)

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
