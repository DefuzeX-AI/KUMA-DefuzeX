import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;
import io.opentelemetry.exporter.otlp.http.trace.OtlpHttpSpanExporter;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Collection;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** Persistent Java Agent with invocation-scoped OTLP export and explicit tool observations. */
public final class ExampleAgent {
    /** Observe actual exporter outcomes; a flush barrier alone does not prove delivery. */
    private static final class CheckedExporter implements SpanExporter {
        private final SpanExporter delegate;
        private final AtomicBoolean failed = new AtomicBoolean();

        /** Wrap the invocation-owned exporter without logging request data or headers. */
        CheckedExporter(SpanExporter delegate) {
            this.delegate = delegate;
        }

        /** Preserve the exporter result and remember any failed batch for this invocation. */
        @Override
        public CompletableResultCode export(Collection<SpanData> spans) {
            CompletableResultCode result = delegate.export(spans);
            return result.whenComplete(() -> {
                if (!result.isSuccess()) {
                    failed.set(true);
                }
            });
        }

        /** Forward flush to the owned transport. */
        @Override
        public CompletableResultCode flush() {
            return delegate.flush();
        }

        /** Close the owned transport when the provider shuts down. */
        @Override
        public CompletableResultCode shutdown() {
            return delegate.shutdown();
        }
    }

    /** Read endpoint, bearer and input as three UTF-8 lines per invocation; never echo the bearer. */
    public static void main(String[] args) throws Exception {
        BufferedReader reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        int invocation = 0;
        String endpoint;
        while ((endpoint = reader.readLine()) != null) {
            String authorization = reader.readLine();
            String input = reader.readLine();
            if (authorization == null || input == null) {
                throw new IllegalArgumentException("Incomplete invocation");
            }
            int left = Integer.parseInt(input);
            int result = execute(endpoint, authorization, left);
            invocation++;
            System.out.println("{\"sum\":" + result + ",\"invocation\":" + invocation + "}");
            System.out.flush();
        }
    }

    /** Capture one real local Agent invocation, observed model-adapter call, and tool execution. */
    private static int execute(String endpoint, String authorization, int left) throws Exception {
        CheckedExporter exporter = new CheckedExporter(OtlpHttpSpanExporter.builder()
            .setEndpoint(endpoint)
            .addHeader("Authorization", authorization)
            .setTimeout(Duration.ofSeconds(3))
            .build());
        SdkTracerProvider provider = SdkTracerProvider.builder()
            .setResource(Resource.create(Attributes.of(
                AttributeKey.stringKey("service.name"), "kuma-java-example",
                AttributeKey.stringKey("telemetry.sdk.language"), "java")))
            .addSpanProcessor(SimpleSpanProcessor.create(exporter))
            .build();
        OpenTelemetrySdk sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
        Tracer tracer = sdk.getTracer("kuma.java.example");
        boolean operationFailed = false;
        try {
            Span agent = tracer.spanBuilder("invoke_agent local-add").startSpan();
            agent.setAttribute("gen_ai.operation.name", "invoke_agent");
            try (Scope ignored = agent.makeCurrent()) {
                // This local model adapter is a deterministic test fixture. Replace its body
                // with your real model client; record the values actually sent and returned.
                Span model = tracer.spanBuilder("chat local-fixture").startSpan();
                model.setAttribute("gen_ai.operation.name", "chat");
                model.setAttribute("gen_ai.request.model", "deterministic-local-fixture");
                try (Scope modelScope = model.makeCurrent()) {
                    String messages = "[{\"role\":\"user\",\"content\":\"Add " + left + " and 3\"}]";
                    model.setAttribute("gen_ai.input.messages", messages);
                    model.setAttribute("gen_ai.output.messages", "[{\"role\":\"assistant\",\"content\":\"Use add\"}]");
                    model.setStatus(StatusCode.OK);
                } catch (RuntimeException error) {
                    model.setStatus(StatusCode.ERROR);
                    throw error;
                } finally {
                    model.end();
                }
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
                agent.setStatus(StatusCode.OK);
                return result;
            } catch (RuntimeException error) {
                agent.setStatus(StatusCode.ERROR);
                throw error;
            } finally {
                agent.end();
            }
        } catch (RuntimeException error) {
            operationFailed = true;
            throw error;
        } finally {
            // Flush completion is part of the invocation boundary. Python seals its
            // step only after reading the result emitted by main().
            boolean delivered;
            try {
                delivered = provider.forceFlush().join(4, TimeUnit.SECONDS).isSuccess()
                    && !exporter.failed.get();
            } finally {
                provider.close();
            }
            if (!delivered && !operationFailed) {
                throw new IllegalStateException("Trace export failed; no result submitted");
            }
        }
    }
}
