# KUMA Agent Integration Demo

This example shows how to connect an Agent to the public KUMA Run protocol.

## Modes

- Mock mode is free, deterministic, local, and requires no API key.
- Hosted mode connects to the hosted KUMA service and may consume credits.

## Quickstart

From the KUMA repository root:

    .\.venv\Scripts\python.exe examples/agent_integration_demo/app.py

Expected output includes:

    mode=mock
    cost=free_local_no_api_call
    run_state=completed
    history_items=1
    result={...}

Mock mode does not contact the KUMA backend or consume hosted evaluation credits.

## Run lifecycle

The example creates a KUMA Run, gets an input with run.get_input(), passes it to call_your_agent(), submits the result with run.submit(), and prints the final state and history.

Replace call_your_agent() with the Agent framework or client you want to use. Keep the returned result JSON-compatible.

## Hosted mode

Hosted mode checks required configuration before creating a hosted Run.

Set these environment variables outside source control:

    KUMA_API_KEY=YOUR_API_KEY
    KUMA_REPO_PATH=C:\path\to\repository

Then run:

    .\.venv\Scripts\python.exe examples/agent_integration_demo/app.py --hosted

Hosted evaluation may contact the KUMA backend and may consume credits. Never commit API keys or populated environment files.

## Agent profile

agent-profile.md contains the profile used by the example. Replace its behavior description when adapting the example to a real Agent use case.

## Verification

Syntax check:

    python -m py_compile examples/agent_integration_demo/app.py

Free local verification:

    .\.venv\Scripts\python.exe examples/agent_integration_demo/app.py

The local path should complete without credentials or network access.

This example does not claim that cancelling a run refunds already-generated Cases.
