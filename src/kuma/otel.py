"""Optional OpenTelemetry SDK adapter for in-process Trace Evidence."""

from __future__ import annotations

import threading
import weakref
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from .errors import ConfigurationError
from .evidence.trace import TraceEvidenceCapture, TraceEvidenceLimits


def _resolve_log_export_api(
    export_module: Any,
) -> tuple[type[Any], type[Any], type[Any]]:
    """Resolve the OTel Logs exporter rename across supported SDK releases.

    OpenTelemetry Logs remains a development signal and renamed ``LogExporter``
    and ``LogExportResult`` in 1.39 while retaining the same processor extension
    point. KUMA supports the declared 1.30--1.x range by preferring the new names
    and falling back to the old pair as one indivisible API generation.

    Args:
        export_module: Imported ``opentelemetry.sdk._logs.export`` module.

    Returns:
        Exporter base, result enum, and simple processor classes from one
        supported OTel Logs API generation.

    Raises:
        ImportError: If neither complete API generation is available or the
            processor extension point is missing.

    Postconditions:
        New and old exporter/result names are never mixed, so KUMA returns the
        result enum expected by the installed processor implementation.
    """
    processor = getattr(export_module, "SimpleLogRecordProcessor", None)
    generations = (
        ("LogRecordExporter", "LogRecordExportResult"),
        ("LogExporter", "LogExportResult"),
    )
    for exporter_name, result_name in generations:
        exporter = getattr(export_module, exporter_name, None)
        result = getattr(export_module, result_name, None)
        if isinstance(exporter, type) and isinstance(result, type):
            if not isinstance(processor, type):
                break
            return exporter, result, processor
    raise ImportError("unsupported OpenTelemetry Logs exporter API")


def _otel_import_error_message() -> str:
    """Return an actionable optional-dependency error without internal details."""
    try:
        installed = metadata.version("opentelemetry-sdk")
    except metadata.PackageNotFoundError:
        return 'OpenTelemetry Trace Evidence requires: pip install "kuma-defuzex[otel]"'
    return (
        "OpenTelemetry Trace Evidence is incompatible with installed "
        f"opentelemetry-sdk {installed}; install a supported >=1.30,<2 release "
        'with: pip install --upgrade "kuma-defuzex[otel]"'
    )


try:
    from opentelemetry import trace
    from opentelemetry._logs import get_logger_provider
    from opentelemetry.sdk._logs import export as _otel_log_export
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.sdk.trace.export import (
        SpanExporter,
        SpanExportResult,
    )

    (
        LogRecordExporter,
        LogRecordExportResult,
        SimpleLogRecordProcessor,
    ) = _resolve_log_export_api(_otel_log_export)
except ImportError as exc:  # pragma: no cover - exercised without the optional extra
    raise ImportError(_otel_import_error_message()) from exc


_ATTACH_LOCK = threading.RLock()


@dataclass(slots=True)
class _CaptureAttachment:
    """Track idempotent processors attached to one user-owned OTel provider.

    Attributes:
        capture: Shared bounded capture reused by Runs using this provider.
        trace_processor: Single span processor installed on the tracer provider.
        log_processors: Log processors successfully installed on compatible
            logger providers.
        logger_attempts: Weak provider set preventing duplicate log attachment
            while allowing late compatible logger configuration.
    """

    capture: TraceEvidenceCapture
    trace_processor: TraceEvidenceSpanProcessor
    log_processors: list[Any] = field(default_factory=list)
    logger_attempts: weakref.WeakKeyDictionary[Any, bool] = field(
        default_factory=weakref.WeakKeyDictionary
    )


_ATTACHED_CAPTURES: weakref.WeakKeyDictionary[Any, _CaptureAttachment] = (
    weakref.WeakKeyDictionary()
)


class TraceEvidenceSpanExporter(SpanExporter):
    """Standard exporter that never propagates capture failures to user code."""

    def __init__(self, capture: TraceEvidenceCapture) -> None:
        """Bind the standard exporter extension point to one bounded capture."""
        self.capture = capture

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        """Export ended spans that were not associated through ``on_start``."""
        return self._export(spans, registered=False)

    def export_registered(self, spans: Sequence[Any]) -> SpanExportResult:
        """Export a registered span using its captured Run association."""
        return self._export(spans, registered=True)

    def _export(self, spans: Sequence[Any], *, registered: bool) -> SpanExportResult:
        """Isolate each span failure and record only a stable value-free reason."""
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
                except Exception:
                    continue
        return SpanExportResult.FAILURE if failed else SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush accepted telemetry within the caller-provided time bound."""
        del timeout_millis
        return True

    def shutdown(self) -> None:
        """Finish this telemetry adapter without changing the user's provider."""
        return None


class TraceEvidenceSpanProcessor(SpanProcessor):
    """Bind spans to the active step at start, including worker threads."""

    def __init__(self, exporter: TraceEvidenceSpanExporter) -> None:
        """Bind one exporter to OTel span lifecycle callbacks."""
        self.exporter = exporter

    def on_start(self, span: Any, parent_context: Any | None = None) -> None:
        """Register a started span for later same-process Run association."""
        del parent_context
        try:
            self.exporter.capture.register_span(span)
        except Exception:
            try:
                self.exporter.capture.record_failure("trace_export_failed")
            except Exception:
                return

    def on_end(self, span: Any) -> None:
        """Export an ended span while isolating instrumentation failures."""
        self.exporter.export_registered((span,))

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush accepted telemetry within the caller-provided time bound."""
        return self.exporter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        """Finish this telemetry adapter without changing the user's provider."""
        self.exporter.shutdown()


class TraceEvidenceLogRecordExporter(LogRecordExporter):
    """Standard OTel Logs exporter with per-record capture isolation."""

    def __init__(self, capture: TraceEvidenceCapture) -> None:
        """Bind the OTel Logs exporter extension point to one bounded capture."""
        self.capture = capture

    def export(self, batch: Sequence[Any]) -> LogRecordExportResult:
        """Export each LogRecord independently without retaining raw sensitive text."""
        failed = False
        for record in batch:
            try:
                self.capture.export_log_record(record)
            except Exception:
                failed = True
                try:
                    self.capture.record_log_failure("otel_log_export_failed")
                except Exception:
                    # A failing exporter must not block later LogRecords.
                    continue
        return (
            LogRecordExportResult.FAILURE if failed else LogRecordExportResult.SUCCESS
        )

    def shutdown(self) -> None:
        """Finish this telemetry adapter without changing the user's provider."""
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush accepted telemetry within the caller-provided time bound."""
        del timeout_millis
        return True


def _log_processor_adder(logger_provider: Any) -> Any:
    """Require the official LoggerProvider processor extension point."""
    add_log_processor = getattr(logger_provider, "add_log_record_processor", None)
    if not callable(add_log_processor):
        raise ConfigurationError(
            "OTel log Evidence requires an SDK LoggerProvider with "
            "add_log_record_processor()"
        )
    return add_log_processor


def _new_trace_attachment(
    provider: Any, limits: TraceEvidenceLimits | None
) -> _CaptureAttachment:
    """Attach exactly one span processor and install capture-level flush handling."""
    add_processor = getattr(provider, "add_span_processor", None)
    if not callable(add_processor):
        raise ConfigurationError(
            "Trace Evidence requires an SDK TracerProvider with add_span_processor()"
        )
    capture = TraceEvidenceCapture(limits)
    exporter = TraceEvidenceSpanExporter(capture)
    processor = TraceEvidenceSpanProcessor(exporter)
    add_processor(processor)
    attachment = _CaptureAttachment(capture, processor)

    def force_flush() -> bool:
        """Flush accepted telemetry within the caller-provided time bound."""
        trace_flushed = processor.force_flush()
        for log_processor in tuple(attachment.log_processors):
            try:
                if not log_processor.force_flush():
                    capture.record_log_failure("otel_log_capture_failed")
            except Exception:
                capture.record_log_failure("otel_log_capture_failed")
        return trace_flushed

    capture._set_force_flush(force_flush)
    return attachment


def _attach_logger_once(attachment: _CaptureAttachment, logger_provider: Any) -> None:
    """Attach one deduplicated log processor, skipping unsafe proxy wrappers."""
    add_log_processor = _log_processor_adder(logger_provider)
    try:
        previous = attachment.logger_attempts.get(logger_provider)
    except TypeError:
        # Automatic mode cannot safely deduplicate this logger wrapper. Keep the
        # already attached trace capture and skip logs without changing the Run.
        return
    if previous is True:
        return
    if previous is False:
        raise ConfigurationError("Automatic OTel log Evidence attachment failed")

    log_processor = SimpleLogRecordProcessor(
        TraceEvidenceLogRecordExporter(attachment.capture)
    )
    try:
        attachment.logger_attempts[logger_provider] = False
    except TypeError:
        return
    # Register before the provider call: a provider may retain the processor and
    # then raise, so retrying would otherwise accumulate duplicate processors.
    attachment.log_processors.append(log_processor)
    add_log_processor(log_processor)
    attachment.logger_attempts[logger_provider] = True


def _attach_logger_explicit(
    attachment: _CaptureAttachment, logger_provider: Any
) -> None:
    """Attach the explicitly requested LoggerProvider or fail configuration."""
    add_log_processor = _log_processor_adder(logger_provider)
    log_processor = SimpleLogRecordProcessor(
        TraceEvidenceLogRecordExporter(attachment.capture)
    )
    attachment.log_processors.append(log_processor)
    add_log_processor(log_processor)


def _attach_trace_evidence(
    provider: Any,
    *,
    logger_provider: Any | None,
    limits: TraceEvidenceLimits | None,
) -> _CaptureAttachment:
    """Attach explicit trace and optional log processors without replacing providers."""
    if logger_provider is not None:
        _log_processor_adder(logger_provider)
    attachment = _new_trace_attachment(provider, limits)
    if logger_provider is not None:
        _attach_logger_explicit(attachment, logger_provider)
    return attachment


def configure_trace_evidence(
    tracer_provider: Any | None = None,
    *,
    logger_provider: Any | None = None,
    limits: TraceEvidenceLimits | None = None,
) -> TraceEvidenceCapture:
    """Explicitly attach bounded KUMA capture to existing OTel SDK providers.

    Args:
        tracer_provider: Existing OpenTelemetry SDK ``TracerProvider`` exposing
            ``add_span_processor``. ``None`` selects the current global provider;
            the function never installs or replaces a provider.
        logger_provider: Optional existing OTel SDK ``LoggerProvider`` exposing
            ``add_log_record_processor``. Supply it to capture native OTel logs;
            ``None`` leaves logs unattached in explicit mode.
        limits: Capture budgets for spans, attributes, events, text, logs, and
            total per-Run bytes. ``None`` uses :class:`TraceEvidenceLimits`.

    Returns:
        Thread-safe :class:`TraceEvidenceCapture` to pass as
        ``create_run(trace_evidence=...)``. Reusing the same compatible provider
        returns/records through one attachment rather than replacing user setup.

    Raises:
        ConfigurationError: If a provider lacks the required official extension
            point, cannot satisfy explicit weak-reference/idempotency guarantees,
            or the supplied limits are invalid.

    Preconditions:
        Install the optional ``kuma-defuzex[otel]`` extra and configure the OTel
        SDK provider/instrumentation that produces spans. This function does not
        create spans by itself.

    Postconditions:
        The provider retains all existing processors and gains one KUMA span
        processor plus, when supplied, one log processor. No global provider is
        reset. The returned capture is ready for Run step association.

    Side Effects:
        Mutates the supplied providers through their standard processor APIs.
        It performs no network export and starts no OTLP receiver.

    Security/Privacy:
        KUMA keeps only allowlisted bounded metadata/hashes. Raw prompts,
        completions, log bodies, tool arguments, source, and credentials are not
        retained by default. ``allow_sensitive`` cannot bypass this allowlist.
    """

    provider = tracer_provider or trace.get_tracer_provider()
    with _ATTACH_LOCK:
        attachment = _attach_trace_evidence(
            provider,
            logger_provider=logger_provider,
            limits=limits,
        )
        # Automatic mode must be able to reuse a provider without accumulating
        # processors; non-weak-referenceable custom wrappers remain explicit-only.
        with suppress(TypeError):
            _ATTACHED_CAPTURES[provider] = attachment
        return attachment.capture


def _automatic_trace_evidence() -> TraceEvidenceCapture | None:
    """Reuse or attach capture when the global OTel SDK is already configured."""

    provider = trace.get_tracer_provider()
    if not callable(getattr(provider, "add_span_processor", None)):
        return None
    with _ATTACH_LOCK:
        try:
            attachment = _ATTACHED_CAPTURES.get(provider)
        except TypeError:
            return None
        if attachment is None:
            attachment = _new_trace_attachment(provider, None)
            _ATTACHED_CAPTURES[provider] = attachment
        try:
            logger_provider = get_logger_provider()
        except Exception:
            logger_provider = None
        if callable(getattr(logger_provider, "add_log_record_processor", None)):
            _attach_logger_once(attachment, logger_provider)
        return attachment.capture


__all__ = [
    "TraceEvidenceCapture",
    "TraceEvidenceLimits",
    "TraceEvidenceLogRecordExporter",
    "TraceEvidenceSpanExporter",
    "TraceEvidenceSpanProcessor",
    "configure_trace_evidence",
]
