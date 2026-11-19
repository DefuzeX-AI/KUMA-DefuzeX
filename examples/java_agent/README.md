# Java Agent with OTLP Trace evidence

Run one persistent Java process through two KUMA Inputs. The sample records an
Agent span, a deterministic local model-adapter fixture, and a real addition tool
for each Input. It uses the official Java OpenTelemetry SDK and OTLP/HTTP exporter.
Its model fixture uses no hosted model, account, API key, or paid Case/Judge service.

## Do I need to change my Agent?

You do not need to rewrite your Agent's business logic, but you do need an
invocation adapter and a telemetry export path. This is a **Python KUMA SDK +
Java OpenTelemetry bridge**, not a Java-native KUMA SDK. Use the normal repository
and release; no long-lived Java-specific branch is required.

- Already emitting compatible model/tool OTel spans? Reuse those observations and
  connect their exporter to the current Input's receiver lease.
- Only emitting generic HTTP spans? Those do not provide model request/response
  bodies or executed-tool arguments/results.
- No compatible instrumentation? Add spans at the real model-client call and
  actual tool-execution boundaries, without changing their business behavior.
  Attaching a Java agent does not guarantee automatic capture of all these bodies.

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

### What to adapt

| Layer | Starting point in this example | Your responsibility |
| --- | --- | --- |
| Python harness | [run.py](run.py): `main`, `_local_case`, `_start_java`, `_verify_history` | Select Inputs and providers, pass each lease to Java, decode the actual result, submit it, and clean up. Replace the addition-specific result assertions and input fixtures. |
| Java invocation adapter and exporter | [ExampleAgent.java](src/main/java/ExampleAgent.java): `main`, `execute`, `CheckedExporter` | Call your Agent entry point; configure per-Input export; preserve checked flush and failure handling. Keep the JVM and Agent session alive across Inputs. |
| Agent instrumentation | Model and tool spans inside `ExampleAgent.execute` | Reuse compatible spans, or instrument the real model request/response and actual tool arguments/result. Never fabricate bodies from a final answer. |

### Migrate the working fixture

1. **Run the local example above first.** Keep `judge=False` while adapting your
   Agent. Its deterministic model adapter is a fixture, not a hosted model client;
   replacing it with a real client may incur your provider's normal charges.
2. **Replace the invocation boundary.** In Java `main`, replace
   `Integer.parseInt(input)`, the addition-specific `execute` call and result
   encoding with your Agent's input and output protocol. In Python `main`, update
   the three-line write, JSON decode and addition assertions together. The sample
   protocol is endpoint, Authorization value, integer Input, each on its own UTF-8
   line; Java returns one JSON line. It is not a general Agent protocol. Use suitable
   framing for multiline/structured Inputs, and keep stdout reserved for results.
   Construct reusable Agent/session state outside the invocation loop, not anew
   for each Input.
3. **Connect telemetry for that Input.** Pass `receiver.capture` to
   `create_run(trace_evidence=...)`. Keep this order for each Input:

   `get_input → receiver.step → invoke Agent + end spans + checked flush → exit step → submit`

   The endpoint and bearer must come from that exact `receiver.step()`; never
   cache them for the next Input or put the bearer in prompts, logs or Evidence.
   The following excerpt is copied from `ExampleAgent.execute`. `endpoint` is
   `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`; `authorization` is the header value
   after removing the `Authorization=` prefix from
   `OTEL_EXPORTER_OTLP_TRACES_HEADERS`. Keep the example's `CheckedExporter`
   helper and imports; this is an excerpt, not a standalone class.

   ```java
   CheckedExporter exporter = new CheckedExporter(OtlpHttpSpanExporter.builder()
       .setEndpoint(endpoint)
       .addHeader("Authorization", authorization)
       .setTimeout(Duration.ofSeconds(3))
       .build());
   ```

   The example attaches it through `SimpleSpanProcessor.create(exporter)` to an
   explicitly owned `SdkTracerProvider`. If your application already has a provider,
   reuse its instrumentation/export pipeline rather than registering a conflicting
   global provider. The example uses `build()`, not global registration. Its
   provider is invocation-owned while its JVM persists. A persistent provider
   needs an equivalent fixed-header export path per Input and a flush barrier
   before changing that path; never retag queued spans with the next Input's bearer.
   Do not close an application-owned provider after each invocation.
4. **Replace fixture observations with real observations.** Replace the
   `chat local-fixture` block with instrumentation around your real model call.
   Record the request actually sent and response actually received, using the
   attributes below. Wrap actual tool execution separately from a model's proposed
   tool call. This tool block is copied from the compiled example; `tracer` and
   `left` are existing variables in `execute`. Replace the addition and its
   JSON encoding with your actual tool and values, keeping error status and
   `finally` cleanup:

   ```java
   Span tool = tracer.spanBuilder("execute_tool add").startSpan();
   tool.setAttribute("gen_ai.operation.name", "execute_tool");
   tool.setAttribute("gen_ai.tool.name", "add");
   tool.setAttribute("gen_ai.tool.call.arguments", "{\"left\":" + left + ",\"right\":3}");
   int result;
   try (Scope toolScope = tool.makeCurrent()) {
       result = Math.addExact(left, 3);
       tool.setAttribute("gen_ai.tool.call.result", "{\"sum\":" + result + "}");
       tool.setStatus(StatusCode.OK);
   } catch (RuntimeException error) {
       tool.setStatus(StatusCode.ERROR);
       throw error;
   } finally {
       tool.end();
   }
   ```

5. **Keep delivery and lifecycle checks.** In `execute`, preserve the full
   `finally` block: end spans, wait for `provider.forceFlush()`, check
   `CheckedExporter.failed`, and close only the owned provider. A flush barrier
   alone does not prove export succeeded. Return a successful result only after
   these checks; on export failure/timeout, do not submit a success. Preserve
   Python's bounded result wait, process cleanup and cancellation of an unfinished
   Run. Keep actual tool failures as failures, without inventing a result.
6. **Verify your own Agent before enabling hosted evaluation.** Adapt
   `_verify_history` as described below. Then configure your real Agent Profile
   and the normal Case/Judge providers in the Python harness. See the
   [SDK guide](../../docs/sdk-guide.md); hosted generation/evaluation can use
   service credit. KUMA service credentials stay in Python, not in Java's OTLP
   headers. Do not carry the sample's addition-only assertions into production.

### Required observations and success checks

| Operation | Span attributes |
| --- | --- |
| Agent invocation | `gen_ai.operation.name=invoke_agent` |
| Model call | `gen_ai.operation.name=chat` or `text_completion`, `gen_ai.request.model`, `gen_ai.input.messages`, `gen_ai.output.messages` |
| Executed tool | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |

Supply structured bodies as JSON attribute strings; use your JSON serializer for
real data rather than concatenating untrusted strings. Record exceptions as span
status `ERROR`. Do not add a tool-result attribute when the tool did not return
one. Preserve parent/context relationships around actual calls.

After `submit`, inspect each item's
`run.history[i].submission.extensions["trace_evidence"]` locally. Follow
`_verify_history` in [run.py](run.py), then extend the checks for your Agent:

- Confirm each Input owns its observations and the session persists across Inputs.
- Check model `model_content.input` and `model_content.output` statuses and
  retained values against the actual request/response, allowing documented
  redaction. Do not substitute the fixture's "Use add" response.
- Check tool `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result` in span
  attributes against the executed tool and its real outcome.
- Inspect capture reasons and content statuses. A printed final answer, HTTP 200,
  or three spans alone is not proof of complete capture. Partial, redacted,
  omitted or rejected content must not be described as fully captured.
- Do not print sensitive bodies or exporter headers while diagnosing.

`tools/verify_java_agent.py` verifies only the bundled local fixture, its two
Inputs, rejected export and executed-tool error. It does **not** validate your
replacement Agent, call a real hosted model, or demonstrate hosted Judge success.
Hosted upload additionally requires the service's negotiated Trace support.

KUMA applies its existing privacy filters and limits before Evidence retention.
Missing, redacted, rejected or oversized content receives explicit capture
statuses and reasons. The receiver accepts OTLP/HTTP Protobuf, optional gzip,
at most 1000 spans per batch, and bounded request bodies. Each accepted connection
has a two-second total HTTP header/body read deadline; this is not a two-second
limit on Agent execution. Its address is `127.0.0.1`: Java and Python need a shared
loopback network namespace, even when using containers.

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
