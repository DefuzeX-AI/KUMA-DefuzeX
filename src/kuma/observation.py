"""Explicit local observation independent of evaluation, accounts and transport."""

from __future__ import annotations

import copy
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ._version import __version__
from .errors import ConfigurationError, ValidationError
from .evidence.observation_capture import ObservationBuffer
from .evidence.observation_otel import ACTIVE_OBSERVATION, attach, span_key
from .evidence.trace import TraceEvidenceLimits
from .repository.privacy import scan_sensitive_text


def _external_id(value: str | None) -> str | None:
    """Validate optional correlation labels without echoing rejected identifiers."""
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None
        or scan_sensitive_text(value, location="observation_id")
    ):
        raise ValidationError(
            "External observation IDs must be safe ASCII identifiers of 1-128 characters",
            code="observation_invalid",
        )
    return value


class ObservationSession:
    """Observe spans during one caller-owned execution without evaluating it.

    Use ``kuma.observe`` to construct and enter this one-shot sync/async context.
    ``export()`` and ``render_text()`` become available after exit. Capture failures
    degrade the report, never suppress or replace an exception from the Agent.
    No credentials, Case, Judge, pricing, token-cost estimates or network are used.
    User instrumentation/exporters keep their own behavior and ownership.
    """

    def __init__(
        self,
        *,
        tracer_provider: Any = None,
        external_run_id: str | None = None,
        external_invocation_id: str | None = None,
        limits: TraceEvidenceLimits | None = None,
    ) -> None:
        """Prepare an inactive session; arguments and guarantees match ``observe``.

        Construction validates only local options and creates an opaque UUID;
        provider attachment is deferred until entry. No files or keys are read.
        """
        chosen = TraceEvidenceLimits() if limits is None else limits
        if not isinstance(chosen, TraceEvidenceLimits) or chosen.max_total_bytes < 2048:
            raise ConfigurationError("Observation limits require at least 2048 bytes")
        self._metadata = {
            "schema_version": "kuma.observation.v1",
            "observation_id": "obs_" + uuid.uuid4().hex,
            "external_run_id": _external_id(external_run_id),
            "external_invocation_id": _external_id(external_invocation_id),
            "execution_status": "unknown",
            "evaluation_status": "not_performed",
            "duration_ms": None,
            "sdk_version": __version__,
        }
        self._provider = tracer_provider
        self._processor = None
        self._buffer = ObservationBuffer(chosen)
        self._lock = threading.RLock()
        self._pending: set[tuple[int, int]] = set()
        self._state = "new"
        self._token = None
        self._started = 0.0
        self._result: dict[str, Any] | None = None

    def __enter__(self) -> ObservationSession:
        """Start one context-local capture window without altering global providers.

        Raises ConfigurationError if reused. An absent/incompatible provider
        records unavailable capture, but still permits the caller's Agent block.
        The calling context must also perform exit, as with ordinary contextvars.
        """
        with self._lock:
            if self._state != "new":
                raise ConfigurationError(
                    "An observation session can only be entered once"
                )
            self._processor = attach(self._provider)
            self._provider = None
            self._state = "active"
            self._started = time.monotonic()
            if self._processor is None:
                self._buffer.loss("trace_capture_failed")
            else:
                with self._processor.lock:
                    self._processor.sessions.add(self)
            self._token = ACTIVE_OBSERVATION.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        """Finish capture synchronously and propagate the original Agent exception.

        Normal return means execution completed, not evaluation passed. Exception
        means failed; cancellation/interrupt means aborted. Only this status is
        retained, never exception values, reprs or stack frames. Open spans become
        explicit window loss. No exporter flush, file write or upload is performed.
        """
        with self._lock:
            if self._state != "active":
                return False
            self._state = "closed"
            if self._processor is not None:
                with self._processor.lock:
                    self._processor.sessions.discard(self)
            ACTIVE_OBSERVATION.reset(self._token)
            self._token = None
            elapsed = int(max(0, time.monotonic() - self._started) * 1000)
            self._metadata["duration_ms"] = elapsed if elapsed <= 86_400_000 else None
            self._metadata["execution_status"] = (
                "completed"
                if exc_type is None
                else "failed"
                if issubclass(exc_type, Exception)
                else "aborted"
            )
            if self._pending:
                self._buffer.loss("trace_span_outside_window", count=len(self._pending))
                self._pending.clear()
            try:
                self._result = self._buffer.finish(self._metadata)
            except Exception:
                # Finalization must not replace an Agent exception or manufacture
                # complete evidence. Keep only trusted metadata and loss counters.
                self._buffer.spans.clear()
                self._buffer.loss("trace_capture_failed")
                self._result = self._buffer._export(self._metadata)
            self._buffer.spans.clear()
        return False

    async def __aenter__(self) -> ObservationSession:
        """Enter without blocking on external I/O; tasks inherit context naturally."""
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        """Close the same window on async completion/cancellation without awaits."""
        return self.__exit__(exc_type, exc, traceback)

    def _start(self, span: Any) -> None:
        """Bound actual span registrations; unmatched/late spans are never reassigned."""
        with self._lock:
            if self._state != "active":
                return
            key = span_key(span)
            if key is None:
                self._buffer.loss("trace_span_context_invalid", count=1)
            elif len(self._pending) >= 10_000:
                self._buffer.loss("trace_span_limit", count=1)
            else:
                self._pending.add(key)

    def _end(self, span: Any) -> None:
        """Map only spans registered to this session; serialize finalization races."""
        with self._lock:
            key = span_key(span)
            if self._state == "active" and key in self._pending:
                self._pending.remove(key)
                observed_before = self._buffer.observed
                try:
                    self._buffer.add(span)
                except Exception:
                    self._buffer.loss(
                        "trace_capture_failed",
                        count=int(self._buffer.observed == observed_before),
                    )

    def export(self) -> dict[str, Any]:
        """Return a detached, redacted plain JSON observation after context exit.

        Returns:
            Closed ``kuma.observation.v1`` document, at most 5 MiB canonical JSON.
            No Case, verdict, report locator, cloud state or charge is invented.
        Raises:
            ConfigurationError: The context has not finished.
        Postconditions:
            Mutating the result cannot change stored capture or subsequent exports.
        """
        with self._lock:
            if self._result is None:
                raise ConfigurationError("Finish the observation before exporting it")
            return copy.deepcopy(self._result)

    def render_text(self) -> str:
        """Return a bounded human-readable timeline of actually captured spans.

        Shows safe span name, operation, duration and status, not raw body text.
        Times are recorded span duration, not inferred queue/model billing time.
        Missing capture is unknown, not proof that no tools ran. Requires exit.
        """
        data = self.export()
        lines = [
            f"Observation {data['observation_id']}",
            f"Execution: {data['execution_status']}; evaluation: not_performed",
            f"Capture: {data['capture_status']}; spans: {len(data['spans'])}",
        ]
        for span in data["spans"]:
            operation = span["attributes"].get("gen_ai.operation.name", "unknown")
            name = str(span["name"]).encode("unicode_escape").decode("ascii")
            lines.append(
                f"- {operation}: {name} [{span['status']}] "
                f"{span['duration_nano'] / 1_000_000:.3f} ms"
            )
        if data["reasons"]:
            lines.append("Gaps: " + ", ".join(data["reasons"]))
        return "\n".join(lines)

    def save(
        self, path: str | os.PathLike[str], *, root: str | os.PathLike[str]
    ) -> Path:
        """Atomically save the finished export in an explicitly authorized directory.

        Args:
            path: New JSON filename, relative to root or absolute inside it.
                Existing files are never overwritten; parent must already exist.
            root: Existing local directory authorizing writes. Links, mount escapes
                and reparse paths fail closed; no repository scanning occurs.
        Returns:
            Absolute published path containing exactly ``export()`` as JSON.
        Raises:
            ConfigurationError: Session unfinished, unsafe path or local I/O failure.
        Side Effects:
            Write/fsync a temporary sibling, atomically publish and clean temporary
            resources. Failure does not alter existing files. No network or upload.
        """
        from .repository.observation_io import save_observation

        return save_observation(self.export(), path, root=root)


def observe(
    *,
    tracer_provider: Any = None,
    external_run_id: str | None = None,
    external_invocation_id: str | None = None,
    limits: TraceEvidenceLimits | None = None,
) -> ObservationSession:
    """Create an opt-in local observation context, without a KUMA Run or account.

    Args:
        tracer_provider: Existing OTel SDK-compatible provider, or None to resolve
            the global provider at entry. KUMA never installs a global provider or
            instrumentation. Unavailable capture does not block Agent execution.
        external_run_id: Optional safe ASCII correlation label, 1-128 characters;
            letters/digits followed by letters/digits/dot/underscore/colon/hyphen.
            Not authentication; do not put credentials or user content in IDs.
        external_invocation_id: Optional invocation label with the same constraints.
        limits: Existing TraceEvidenceLimits, or defaults. max_total_bytes must be
            at least 2048. Export additionally has a hard 5 MiB aggregate cap and
            10000 span cap; body/attribute/privacy limits remain enforced.
    Returns:
        One-shot ObservationSession supporting ``with`` and ``async with``.
    Raises:
        ConfigurationError: Invalid local limits.
        ValidationError: Unsafe or malformed correlation identifiers.
    Preconditions:
        Instrument the caller-owned Agent with a compatible OTel provider to see
        spans. Creating a session alone does not instrument or execute the Agent.
    Postconditions:
        After exit, export/render/save show only observed facts and explicit gaps;
        evaluation_status is always not_performed. Agent exceptions propagate.
    Security/Privacy:
        No credentials, filesystem scanning, Case/Judge, model calls or uploads.
        Reuses canonical Trace allowlists and body redaction. Local save is a
        separate explicit call. Nested contexts capture only their own starts.
    """
    return ObservationSession(
        tracer_provider=tracer_provider,
        external_run_id=external_run_id,
        external_invocation_id=external_invocation_id,
        limits=limits,
    )
