<p align="center">
  <img src="docs/assets/kuma-banner.svg" width="760" alt="KUMA geometric wordmark banner">
</p>

<h1 align="center">KUMA</h1>

<p align="center">
  <strong>KUMA Python SDK</strong><br>
  Knowledge-grounded Universal Measurement for Agents
</p>

<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; <a href="README.zh-CN.md">Simplified Chinese</a>
</p>

KUMA is a framework-neutral Python SDK for evaluating AI Agents. It delivers a
test Case as step-by-step Inputs, captures bounded Evidence, and produces a
Judgment through official or custom Providers. KUMA does not run your Agent or
expose private evaluation logic.

## What you can do

- Run repeatable Case-to-Judgment evaluations.
- Use official services or custom Case and Judge Providers, including fully
  local workflows.
- Capture structured Evidence from results, file changes, logs, and optional
  OpenTelemetry traces.
- Select a versioned Strategy Group and describe the Agent with an Agent Profile.

## Install

Requires Python 3.10 or newer:

```bash
python -m pip install --upgrade kuma-defuzex
```

The package is `kuma-defuzex`; Python imports and the CLI use `kuma`.

## Quick start

Run a deterministic local check without an account, API key, Docker, or network:

```bash
kuma quickstart
```

Successful output starts with:

```text
Local check: PASS
Score: 100/100
Reason: Output exactly matched the published rule.
```

This checks the bundled local example, not your Agent or the hosted Judge.
To evaluate your Agent, follow the [SDK guide](docs/sdk-guide.md).

## Next steps

- [Run an official evaluation](docs/sdk-guide.md) with the full Case and Judge workflow.
- [Observe an Agent locally](docs/observation.md) without a Case, Judge, or
  automatic upload.
- [Try the full-stack example](examples/full_stack/README.md) with KUMA and
  mini-SWE-agent in Docker. It may use service credit and model budget.

## Documentation

[SDK guide](docs/sdk-guide.md) · [Python API reference](docs/api-reference.md) ·
[Versions and releases](docs/releases.md) · [Detailed Judge assessment](docs/judge-assessment.md)

## Project

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Apache License 2.0](LICENSE)
