"""Run a network-free OTel Logs capture smoke in the installed environment."""

from __future__ import annotations

import logging
from importlib import metadata

from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk.trace import TracerProvider

from kuma.otel import configure_trace_evidence


def main() -> int:
    """Verify that the installed supported OTel release captures one log record.

    Preconditions:
        KUMA and one declared-compatible ``opentelemetry-api``/``-sdk`` pair are
        installed. The caller selects the version; this verifier makes no
        package or network changes itself.

    Returns:
        Process exit code zero after one in-process LogRecord is captured.

    Side Effects:
        Temporarily attaches a private Python logger handler and KUMA processors
        to providers owned by this process, then shuts them down before return.

    Security/Privacy:
        Emits only a fixed non-sensitive message and prints only the installed
        SDK version plus a pass marker.
    """
    tracer_provider = TracerProvider()
    logger_provider = LoggerProvider()
    capture = configure_trace_evidence(
        tracer_provider,
        logger_provider=logger_provider,
    )
    handler = LoggingHandler(logger_provider=logger_provider)
    logger = logging.getLogger("kuma.otel.compatibility")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        capture.begin_step("compat-run", "compat-case", "compat-input")
        logger.info("bounded compatibility record")
        prepared = capture.prepare_step(
            "compat-run",
            "compat-case",
            "compat-input",
        )
        if prepared.otel_logs.observed_count != 1:
            raise AssertionError("expected one observed OTel log record")
        if prepared.otel_logs.retained_count != 1:
            raise AssertionError("expected one retained OTel log record")
        prepared.commit()
        capture.finish_run("compat-run", "compat-case")
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger_provider.shutdown()
        tracer_provider.shutdown()
    print(f"OpenTelemetry SDK {metadata.version('opentelemetry-sdk')}: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
