"""Run local tool observation without a model, account, evaluation or network."""

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

import kuma


def main() -> None:
    """Record actual arithmetic tool spans, then print the redacted local timeline."""
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    tracer = provider.get_tracer("local-example")
    with (
        kuma.observe(tracer_provider=provider, external_run_id="local-demo") as session,
        tracer.start_as_current_span(
            "add",
            attributes={
                "openinference.span.kind": "TOOL",
                "tool.name": "add",
                "input.value": '{"a":2,"b":3}',
            },
        ) as span,
    ):
        result = 2 + 3
        span.set_attribute("output.value", str(result))
    print(session.render_text())
    assert session.export()["spans"][0]["attributes"]["gen_ai.tool.call.result"] == 5
    # Optional explicit persistence; choose a NEW filename in an existing root:
    # session.save("observation.json", root=".")


if __name__ == "__main__":
    main()
