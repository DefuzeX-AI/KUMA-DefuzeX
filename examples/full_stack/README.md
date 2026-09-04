# KUMA full-stack user-flow example

[English](README.md) | [简体中文](README.zh-CN.md)

This guide covers only the supplied mini-SWE-agent example. General SDK installation, API key setup, Agent Profile format, Run protocol, Evidence, OpenTelemetry, Docker boundaries, and troubleshooting live in the canonical [English guide](../../docs/sdk-guide.md) and [简体中文指南](../../docs/sdk-guide.zh-CN.md).

## What this example runs

The example combines the KUMA SDK and mini-SWE-agent in one Docker container. It requests an official Case and Judge, executes each Case step against the mounted workspace, records bounded Trace and log Evidence, and writes the public Judge result locally.

Two entry points are provided:

- [`Dockerfile.user-flow`](Dockerfile.user-flow) with [`docker_user_flow.py`](docker_user_flow.py) for a direct container run.
- [`kuma_real_user_flow.ipynb`](kuma_real_user_flow.ipynb) for the guided Windows/WSL flow.

Both paths invoke real external services and may incur model or service cost.

## Prepare the workspace

Use a disposable or fully committed Git workspace. The direct Docker example expects the mounted root to contain:

- `agent-profile.md` in the accepted format;
- `calculator.py`, the only source file this example may modify;
- tests runnable with `python -m unittest discover -v`.

Export these values before running the example:

- `KUMA_BASE_URL`
- `KUMA_API_KEY`
- `DEEPSEEK_API_KEY`

Do not place populated credentials in the workspace or repository.
Configure only the public KUMA Backend URL; this example never needs a private Core address.

## Build the image

From the SDK repository root:

```bash
docker build -f examples/full_stack/Dockerfile.user-flow -t kuma-user-flow .
```

## Run the container

Windows PowerShell:

```powershell
$workspace = (Resolve-Path "C:\path\to\workspace").Path
docker run --rm `
  --env KUMA_BASE_URL `
  --env KUMA_API_KEY `
  --env DEEPSEEK_API_KEY `
  --mount "type=bind,source=$workspace,target=/workspace" `
  kuma-user-flow
```

Linux or macOS:

```bash
workspace="$(pwd)"
docker run --rm \
  --env KUMA_BASE_URL \
  --env KUMA_API_KEY \
  --env DEEPSEEK_API_KEY \
  --mount "type=bind,source=$workspace,target=/workspace" \
  kuma-user-flow
```

The script rejects non-Docker execution and missing environment variables. It also blocks out-of-scope source changes and fails when the Agent does not submit successfully or the final Judge report is absent.

## Run the Notebook

The Notebook requires Windows, WSL, Jupyter, the two API keys above, and the configured public base URL. Set environment variables before starting Jupyter, open [`kuma_real_user_flow.ipynb`](kuma_real_user_flow.ipynb), then follow its cells to select the Agent workspace.

The Notebook can modify the selected repository. Choose only a disposable workspace or one with all prior work committed.

## Outputs

The direct flow writes example artifacts under `.kuma/mini-swe-agent/` in the mounted workspace:

- compact Agent trajectory and verification Evidence;
- per-step unittest logs;
- the final public `judge-report.json`.

It prints the official Case Inputs, final report, captured span names, final `calculator.py`, and artifact path. These outputs are example-specific; general result handling is documented in the canonical SDK guides.
