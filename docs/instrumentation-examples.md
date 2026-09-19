# Runnable instrumented Agent examples

These examples accompany unreleased KUMA 0.3.0. Public 0.2.8 does not include
`observe()` or this capture mapping. Use this candidate's **source checkout**;
the `examples/` directory is not installed by a wheel.

```sh
python -m venv .venv-examples
# Activate the virtual environment using your platform's normal command.
python -m pip install -e . -r examples/requirements-instrumentation.txt
python examples/instrumented_observation.py --framework openai
python examples/instrumented_observation.py --framework langchain
python examples/instrumented_observation.py --framework langgraph
```

Each command prints `{"result":"5","observation":...}`. They require no real
API key or KUMA account, block outbound sockets, make no uploads, and never call
CaseGen/Judge. Dependency installation itself downloads packages; execution does
not. Use a fresh standalone process, not one already owning global library
instrumentation. Each example explicitly installs/removes its instrumentor and
owns its local TracerProvider; KUMA does neither automatically.

## What is real, what is replaced

| Framework | Real execution | Only external model boundary replaced | Verified normalized capture |
| --- | --- | --- | --- |
| OpenAI | Official Python client request construction, response parsing, OpenInference OpenAI instrumentor | HTTP mock returns one deterministic Chat Completions response; placeholder key never leaves process | 1 model span; model name, input/output, usage 6/1/7 tokens |
| LangChain | RunnableLambda -> real `@tool` addition -> real chat-model callback stack; LangChain instrumentor | Framework `FakeListChatModel` returns `5` | 3 spans: workflow/tool/model; tool args `{a:2,b:3}` and result `5` |
| LangGraph | Compiled two-node StateGraph, actual tool, callback nesting and final state | Same framework fake chat model | 5 spans: graph/two nodes/tool/model, retained parent relationships |

No handcrafted OTel spans stand in for framework instrumentation. The model
fixtures are deterministic tests, not claims of model quality or live-provider
support. OpenAI uses Chat Completions specifically; Responses, streaming, async
and other integrations are **not** claimed by this matrix.
The official [OpenAI SDK documentation](https://developers.openai.com/api/docs/libraries)
describes its client; the exact installed SDK signature and executed test, not a
generic documentation example, establish this matrix's HTTP injection behavior.

## Exact supported test matrix

Verified on **2026-09-19**.

`examples/requirements-instrumentation.txt` is the authoritative tested pin set:

- Python 3.13.7 on Windows in this author run.
- OpenAI 3.16.2 with its `httpx2` 2.13.0 client (not interchangeable with the
  separately pinned `httpx` 0.28.1 used by other dependencies).
- LangChain Core 1.6.3; LangGraph 1.2.11.
- OpenInference OpenAI 0.1.60; LangChain 0.1.76; shared instrumentation 0.1.65;
  semantic conventions 0.1.38.
- OpenTelemetry API/SDK 1.44.0.

These optional dependencies are not added to base KUMA. This is one verified
combination, not a guarantee that every past/future version combination works.
Changing versions requires rerunning the focused matrix.

## Understand completeness and privacy

All three captures currently report `partial`, with
`trace_attribute_filtered` / `trace_attribute_not_allowlisted`. Frameworks emit
extra metadata outside KUMA's privacy allowlist; filtering is counted rather
than silently discarded. All observed spans and parent topology remain retained
in these examples, and the supported model/tool bodies are present. Complete
topology does not imply complete content. Execution `completed` is distinct
from evaluation `not_performed`; these commands do not produce a Judge verdict.

Local recognized-secret redaction and bounded whole-field omission still apply.
Instrumentation must actually record an input/output for it to be retained;
KUMA cannot recover hidden or disabled content. Read/export the local report
before opting into cloud storage or evaluation; see [local observation](observation.md).
User instrumentation/exporters in real applications may have their own network
behavior; these examples install no exporter and explicitly forbid sockets.

Without optional packages, base KUMA remains importable. Local observation does
not fabricate spans: caller code can finish while capture reports failed with
an unavailable-provider reason and evaluation remains not performed.
