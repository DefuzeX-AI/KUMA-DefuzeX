# KUMA Single Agent Template

This framework-neutral template connects one Agent call to the public KUMA `Run` protocol. It contains no model client, Agent framework, production transport mock, service credential, or deployment logic.

## Ownership boundary

Template-owned code:

- creates the Run through public `kuma.create_run()`;
- alternates `get_input()` and `submit()`;
- validates Agent output as non-empty finite JSON;
- truthfully submits timeout/failure status before returning a non-zero exit;
- prints final Run state, History size, Submission status, and public report.

User-owned code:

- replace only `call_your_agent(test_input, *, timeout_seconds)` in [`app.py`](app.py);
- add the chosen Agent runtime/client dependency to [`requirements.txt`](requirements.txt);
- enforce `timeout_seconds` in that framework and raise `TimeoutError` on expiry;
- return only a JSON-compatible result and allow real Agent exceptions to propagate.

Do not change the Run protocol to fit an Agent framework. Adapt the framework inside `call_your_agent`.

## Local deterministic Quickstart

From the SDK repository root, create a clean environment and install this checkout:

```bash
python -m venv .venv-template
.venv-template\Scripts\python.exe -m pip install .
.venv-template\Scripts\python.exe examples/single_agent_template/app.py
```

The commands above are for Windows PowerShell. On Linux/macOS, replace `.venv-template\Scripts\python.exe` with `.venv-template/bin/python`.

Expected success output:

```text
run_state=completed
history_items=1
last_submission_status=completed
report=None
result_link=None
```

The default path uses a temporary repository, deterministic custom Case, `judge=False`, and the fake body of `call_your_agent`. It does not require an API Key and does not contact the Backend or a model.

Run one deterministic Agent exception smoke:

```bash
.venv-template\Scripts\python.exe examples/single_agent_template/app.py --smoke-failure agent
```

It exits with status `1`, records a failed Submission, and prints:

```text
run_state=completed
history_items=1
last_submission_status=failed
report=None
result_link=None
```

Standard error contains only `template_error=agent_failed`; the path never reports success.

## Replace the Agent call

`call_your_agent` is the only user-replaceable function. Its contract is:

```python
def call_your_agent(test_input: Any, *, timeout_seconds: float) -> JsonValue: ...
```

Pass `test_input` unchanged to the real Agent. Configure the Agent framework, process, or HTTP client to enforce `timeout_seconds`. Return the final Agent result as finite JSON data; do not return framework objects, generators, sets, bytes, NaN/Infinity, or a fabricated fallback.

The template handles outcomes as follows:

| Agent outcome | Submission | Process result |
|---|---|---|
| finite, non-empty JSON | `completed` with output | continue; final Judge report when enabled |
| `TimeoutError` | `timeout` with safe error | `template_error=agent_timeout`, exit 1 |
| any other exception | `failed` with safe error | `template_error=agent_failed`, exit 1 |
| `None` or blank text | `failed` with safe error | `template_error=agent_empty_result`, exit 1 |
| not JSON serializable or NaN/Infinity | `failed` with safe error | `template_error=agent_result_not_json`, exit 1 |

Exceptions remain chained inside the adapter boundary; the CLI returns a non-zero status without printing sensitive exception text. SDK failures use their stable `code`/`retryable` values and exit `2`.

## Official Case and Judge

[`environment.example`](environment.example) lists every template environment variable but is not loaded automatically. Export values through your process/container secret mechanism; never commit a populated file.

Official mode requires:

```text
KUMA_USE_OFFICIAL=1
KUMA_API_KEY=<set outside source control>
KUMA_REPO_PATH=<repository evaluated by the Agent>
```

`KUMA_AGENT_PROFILE_PATH` defaults to this directory's [`agent-profile.md`](agent-profile.md). Official mode keeps `allow_local=False` unless `KUMA_ALLOW_LOCAL=1` is explicitly set for local development. The SDK then authenticates only to the Website Backend public API and internally waits on the bounded v2 Case/Judge operations while the Python API remains synchronous.

The Agent receives each `get_input()` value and its validated output/error returns through `submit()`. On success, inspect `run.state`, `run.history`, and `run.report`; the template prints them after completion.

The current public `TestReport` contract does not define a canonical result URL, so the template prints `result_link=None`. It deliberately does not synthesize a URL or introduce a `result_url` field. Add link handling only after that field exists in the accepted public contract.

## Other deterministic failure checks

These never contact an Agent or service:

```bash
.venv-template\Scripts\python.exe examples/single_agent_template/app.py --smoke-failure timeout
.venv-template\Scripts\python.exe examples/single_agent_template/app.py --smoke-failure empty
.venv-template\Scripts\python.exe examples/single_agent_template/app.py --smoke-failure non-json
```
