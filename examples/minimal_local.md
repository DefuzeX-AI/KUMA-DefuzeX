# Minimal local Agent example

From the repository root, install this checkout in an isolated Python environment:

```bash
python -m venv .venv
```

Activate `.venv` before the install and run commands: `source .venv/bin/activate`
on macOS/Linux, or `.venv\Scripts\Activate.ps1` in Windows PowerShell.

Then run:

```bash
python -m pip install .
python examples/minimal_local.py
```

Expected output:

```text
input=Return a bounded maintenance result.
state=completed
submissions=1
submission_status=completed
report=None
```

The bundled Agent and custom Case are deterministic and offline. No API key,
model client, Docker, hosted Case generation, or Judge is used. Temporary files
are removed when the example exits. `state=completed` means the Run protocol
finished, not that the Agent passed an evaluation; `report=None` is intentional.

## Connect your Agent

Replace only `call_your_agent(test_input)` in [minimal_local.py](minimal_local.py).
Pass the input to your Agent and return its actual finite JSON-compatible result.
Add any required client dependency in your own environment, configure credentials
outside source control, and enforce timeouts in your Agent client. A real Agent
may make paid calls even though this example's Case and Judge remain local.

The example records `TimeoutError` as a `timeout` Submission and other Agent
exceptions as `failed`, without submitting sensitive exception text. Both exit
with status 1; inspect `submission_status`, not just `state`. Invalid output or
SDK errors propagate as non-zero failures; an unfinished Run is cancelled to
release local resources. Cancellation is not a refund for any paid calls.

For environment preflight and hosted evaluation, use the existing
[single-Agent template](single_agent_template/README.md) and
[SDK guide](../docs/sdk-guide.md).

Offline regression checks (from the repository root):

```bash
python tools/verify_minimal_local.py
```
