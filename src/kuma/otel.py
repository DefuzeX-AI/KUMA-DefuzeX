"""Optional OpenTelemetry SDK adapter for in-process Trace Evidence."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .errors import ConfigurationError
from .evidence.trace import TraceEvidenceCapture, TraceEvidenceLimits

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.sdk.trace.export import (
        SpanExporter,
        SpanExportResult,
    )
except ImportError as exc:  # pragma: no cover - exercised without the optional extra
    raise ImportError(
        'OpenTelemetry Trace Evidence requires: pip install "kuma[otel]"'
    ) from exc


class TraceEvidenceSpanExporter(SpanExporter):
    """Standard exporter that never propagates capture failures to user code."""

    def __init__(self, capture: TraceEvidenceCapture) -> None:
        self.capture = capture

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        return self._export(spans, registered=False)

    def export_registered(self, spans: Sequence[Any]) -> SpanExportResult:
        return self._export(spans, registered=True)

    def _export(self, spans: Sequence[Any], *, registered: bool) -> SpanExportResult:
        failed = False
        for span in spans:
            try:
                if registered:
                    self.capture.export_registered_span(span)
                else:
                    self.capture.export_span(span)
            except Exception:
                failed = True
                try:
                    self.capture.record_failure("trace_export_failed")
                except Exception:  # nosec B112
                    # The failure is already represented by this export result;
                    # one broken span must not prevent later spans from exporting.
                    continue
        return SpanExportResult.FAILURE if failed else SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True

    def shutdown(self) -> None:
        return None


class TraceEvidenceSpanProcessor(SpanProcessor):
    """Bind spans to the active step at start, including worker threads."""

    def __init__(self, exporter: TraceEvidenceSpanExporter) -> None:
        self.exporter = exporter

    def on_start(self, span: Any, parent_context: Any | None = None) -> None:
        del parent_context
        try:
            self.exporter.capture.register_span(span)
        except Exception:
            try:
                self.exporter.capture.record_failure("trace_export_failed")
            except Exception:
                return

    def on_end(self, span: Any) -> None:
        self.exporter.export_registered((span,))

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.exporter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self.exporter.shutdown()


def configure_trace_evidence(
    tracer_provider: Any | None = None,
    *,
    limits: TraceEvidenceLimits | None = None,
) -> TraceEvidenceCapture:
    """Attach bounded Trace Evidence to an SDK-capable TracerProvider.

    The provider is never replaced or reset, so existing instrumentation and
    processors remain active. Call this once for the provider, then pass the
    returned capture to :func:`kuma.create_run`. If no explicit provider is given,
    the current global provider must expose ``add_span_processor``.
    """

    provider = tracer_provider or trace.get_tracer_provider()
    add_processor = getattr(provider, "add_span_processor", None)
    if not callable(add_processor):
        raise ConfigurationError(
            "Trace Evidence requires an SDK TracerProvider with add_span_processor()"
        )
    capture = TraceEvidenceCapture(limits)
    exporter = TraceEvidenceSpanExporter(capture)
    processor = TraceEvidenceSpanProcessor(exporter)
    add_processor(processor)
    capture._set_force_flush(processor.force_flush)
    return capture


__all__ = [
    "TraceEvidenceCapture",
    "TraceEvidenceLimits",
    "TraceEvidenceSpanExporter",
    "TraceEvidenceSpanProcessor",
    "configure_trace_evidence",
]
