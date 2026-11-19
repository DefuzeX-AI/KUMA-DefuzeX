# Java Agent with OTLP Trace evidence

Run one persistent Java process through two KUMA Inputs. The sample records an
Agent span, a deterministic local model-adapter fixture, and a real addition tool
for each Input. It uses the official Java OpenTelemetry SDK and OTLP/HTTP exporter.
Its model fixture uses no hosted model, account, API key, or paid Case/Judge service.

## Run

Requires JDK 17+, Maven, and Python 3.10+. From the repository root, using an
activated Python virtual environment:

Use a repository checkout or an extracted KUMA source distribution; a wheel
installation alone does not create the example files. For a fresh checkout:

```bash
git clone https://github.com/DefuzeX-AI/KUMA-DefuzeX.git
cd KUMA-DefuzeX
```

```bash
python -m pip install -e ".[otlp]"
mvn -f examples/java_agent/pom.xml package dependency:build-classpath "-Dmdep.outputFile=target/classpath.txt"
python examples/java_agent/run.py
```

Maven downloads the pinned official OpenTelemetry dependencies during the first
build. Subsequent execution sends telemetry only to a temporary loopback listener.

Expected output:

```text
step-1: spans=3 model=recorded tool=recorded
step-2: spans=3 model=recorded tool=recorded
run_state=completed; same_java_process=2_invocations; external_model_calls=0
```

The invocation counter verifies that both Inputs use the same Java process.
Python verifies that each step's model input/output and tool result reach its
committed Evidence. The local example completes with `judge=False`.

To run the same example plus real export-rejection and tool-overflow checks:

```bash
python tools/verify_java_agent.py
```

An exporter error or flush timeout stops the invocation without printing a
successful result. Tool exceptions preserve their actual error status and do not
invent a tool result. The harness cancels any unfinished Run and closes the JVM.

## Connect an existing Java Agent

Keep the Python `create_run` / `get_input` / `submit` lifecycle and replace the
sample Java adapter with the actual Agent entry point. After `get_input`, enter
`receiver.step()`, pass its endpoint and bearer to that invocation's exporter,
execute the Agent, and flush ended spans before returning the result. Exit the
step context before `submit`. Keep the Agent's application/session state alive
across Inputs, even if an exporter is configured per invocation.

The sample's stdin protocol consists of three UTF-8 lines per invocation:
the local traces endpoint, the Authorization header value, and an integer input.
Java writes one JSON result line after flushing. Adapt this transport to your
Agent's supported input format. The bearer is ephemeral local authentication;
keep it out of logs and Agent prompts.

Use an existing Java OpenTelemetry provider when the application already has one.
The sample creates a provider per invocation so its exporter has a fixed step
bearer. Exporters in a persistent provider need an equivalent invocation-scoped
configuration and a flush barrier before changing the bearer. The sample uses
an explicitly owned provider within the persistent Java process.

Record the values the real operations actually use:

| Operation | Span attributes |
| --- | --- |
| Agent invocation | `gen_ai.operation.name=invoke_agent` |
| Model call | `gen_ai.operation.name=chat` or `text_completion`, `gen_ai.request.model`, `gen_ai.input.messages`, `gen_ai.output.messages` |
| Executed tool | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |

Supply structured bodies as JSON attribute strings. Record exceptions as span
status `ERROR` and preserve the real tool outcome. A model's proposed tool call
requires a separate span around actual tool execution. Existing compatible
instrumentation can provide these attributes. Generic HTTP spans alone lack
the tool arguments and model bodies required for this test.

KUMA applies its existing privacy filters and limits before Evidence retention.
Missing, redacted, rejected or oversized content receives the existing explicit
capture statuses and reasons. The receiver supports OTLP/HTTP Protobuf, optional
gzip, a maximum of 1000 spans per batch, and bounded request bodies. Its address
is `127.0.0.1`; Java and Python need a shared loopback network namespace.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `Build first` | Run the Maven command from the repository root. Keep its `-D` argument quoted, including in PowerShell. |
| Missing `otlp` extra | Install `kuma-defuzex[otlp]` in the Python environment running the harness. |
| Connection refused or export timeout | Keep the receiver context open during the invocation. Separate containers do not share loopback by default. |
| Export rejected | Use the endpoint and headers from the current `receiver.step()`. An old bearer is revoked after its Input. Do not log it while diagnosing. |
| Missing model or tool bodies | Configure compatible semantic instrumentation around actual calls. Ordinary HTTP spans do not record these fields automatically. |
| Partial or redacted Evidence | Inspect the recorded capture statuses and reasons; limits and privacy filters can omit content. HTTP success is not a completeness guarantee. |

For hosted evaluation, configure the normal official Case/Judge providers in the
Python harness and keep the same receiver/capture flow. Service credentials stay
with the Python harness. Captured data follows ordinary negotiated Judge upload
support. See [Runtime Trace](../../docs/runtime-trace.md).
