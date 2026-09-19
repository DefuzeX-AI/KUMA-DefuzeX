# KUMA SDK integration guide

This guide assumes you already have a callable Agent. KUMA does not select its
model or start it; KUMA generates public Cases, drives Runs, captures Evidence
and returns Judge reports.

```text
KUMA Case -> user Agent -> output/Evidence -> KUMA Judge
```

Evaluation requests use `https://defuzex.ai/api/agentdefuze`. DefuzeX hosts
Backend, Core MCP, models and databases; users do not start these services.

## 1. Install from a source checkout

Requires Python 3.10 or newer. These commands install public main, not unreleased
private features. A candidate-only example requires that candidate's checkout.

```bash
git clone https://github.com/DefuzeX-AI/KUMA-DefuzeX.git
cd KUMA-DefuzeX
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Linux/macOS:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Verify installation:

```bash
python -c "import kuma; print(kuma.__version__)"
```

## 2. Configure the KUMA API key

Official Case and Judge use `dfx_` keys. Store keys only in environment variables
or the user's credential file.

Windows PowerShell:

```powershell
$env:KUMA_API_KEY = "dfx_<public-id>.<secret>"
kuma whoami
```

Linux/macOS:

```bash
export KUMA_API_KEY="dfx_<public-id>.<secret>"
kuma whoami
```

Alternatively, `kuma.configure(api_key=...)` writes to the current user's credential
directory. Never put keys in source code, notebook output or Git.

The Agent's own model key is separate from KUMA configuration and remains under
the Agent application's control.

## 3. Write an Agent Profile

Official Case Provider requires a nonempty UTF-8 Agent Profile. It describes
the Agent, production scenario, expected behavior and prohibited boundaries as
context for the Strategy Group, not a hidden defect answer or a way to select
or override the Group through prose. Explicit Group coordinates remain
authoritative; omission uses the service catalog default.

Create `agent-profile.md`:

```markdown
---
agent_description: A repository maintenance agent
input_type: text
---

## Production Use Scenario

Maintain a Python repository in a bounded workspace.

## Behaviors to Test

Inspect the repository, follow the Case, make minimal changes, verify them, and report evidence.

## Known Limitations or Prohibited Behaviors

Do not expose credentials, modify tests, add unnecessary dependencies, or access paths outside the repository.
```

YAML front matter and all three second-level headings are required. Empty files
fail before networking. Official Case currently supports only `input_type: text`.

## 4. Connect your Agent

Assign your already initialized Agent adapter to `execute_agent` before running
this example. The adapter accepts the Case payload and returns a JSON-serializable
result. Resolve imports, model configuration and required local resources first.
The local readiness guard deliberately stops the unmodified example before
`create_run`, because official Case generation can consume service credit.
It checks that an adapter was supplied, not whether the Agent will succeed.

```python
from collections.abc import Callable
from typing import Any

from kuma import create_run


# Replace None with your initialized adapter, for example my_agent.invoke.
# Do not replace it with a placeholder that raises NotImplementedError.
execute_agent: Callable[[Any], Any] | None = None
if not callable(execute_agent):
    raise RuntimeError("Configure your Agent adapter before creating a KUMA Run.")


run = create_run(
    repo_path="/path/to/agent-workspace",
    agent_profile_path="agent-profile.md",
    allow_local=True,
    track_files=True,
    upload_diff=False,
    save_local=True,
)

report = None
while (case_input := run.get_input(full=True)) is not None:
    output = execute_agent(case_input.payload)
    report = run.submit(output)

print(report.status, report.confidence)
```

Keep this order: one `get_input()`, one Agent execution, one `submit()`. Do not
advance one Run concurrently from multiple threads.

## 5. Submit logs and Evidence

`track_files=True` captures file Evidence automatically. Pass Agent log files
explicitly when needed:

```python
report = run.submit(
    output,
    logs=["agent-trajectory.json", "test-results.log"],
)
```

`logs` must be an explicit ordered path sequence, not a bare string or bytes.
Relative paths resolve from the Run's repo_path, independent of Jupyter/Python
cwd; successful Evidence stores repository-relative paths. Outside paths, `..`
escapes, other Windows drives/UNC paths, symlinks, missing/unreadable files and
budget excess degrade safely with index-only reasons, not host absolute paths.
Logs are incremental and size-bounded. Never log API keys, environment dumps or
other secrets. The default upload_diff=False does not upload source diffs.

Agents with standard OpenTelemetry Agent/Workflow final output may configure
`kuma.otel.configure_trace_evidence()` and call `run.submit()`. Otherwise, use
explicit `run.submit(output)`. See [Runtime Trace](../../docs/runtime-trace.md).

## 6. Inspect the result

After successful completion:

```python
assert run.state == "report_ready"
assert report is not None
assert all(item.submission.status == "completed" for item in run.history)

print("Run:", run.run_id)
print("Judge:", report.status)
print("Confidence:", report.confidence)
print("Issues:", list(report.issues))
print("Evidence gaps:", list(report.evidence_gaps))
```

`save_local=True` writes local step records under the user's repository `.kuma/`
directory. Private Rubrics, model configuration and server credentials never
enter the SDK or that directory.

## 7. Local development and production

- `allow_local=True` is for development/demos in trusted repositories, not a sandbox.
- Production mode expects SDK and Agent in the same controlled container, without
  allow_local=True.
- Users control the Agent's file, command, network and secret permissions.
  Evidence scanning does not replace isolation or least privilege.

## 8. Internal mini-SWE demonstration

[kuma_real_user_flow.ipynb](./kuma_real_user_flow.ipynb) demonstrates the complete
workflow with mini-SWE-agent and DeepSeek. Its folder picker selects the Agent's
workspace, which it really modifies. Choose a disposable or safely committed
Git checkout only.

The notebook requires Windows, WSL, valid KUMA_API_KEY and DEEPSEEK_API_KEY.
It creates a temporary minimal valid Agent Profile, not a file in the user repository.

## 9. Common errors

- agent_profile_required / agent_profile_invalid: check agent_profile_path,
  front matter, the three required headings and nonempty bodies.
- Authentication/permission errors: verify the dfx_ key and Case/Judge scopes.
- Sensitive-data errors: inspect output, logs, paths and diffs for blocked content.
- log_size_exceeded: reduce logs, never silently truncate the Agent's final output.
- service_busy: follow the returned stable code and recovery guidance; do not
  assume a new Run is safe merely because a request failed.
- Input protocol errors: alternate get_input() and submit() strictly.

See the root [README](../../README.md) for stable error codes, Provider
combinations and all parameters.
