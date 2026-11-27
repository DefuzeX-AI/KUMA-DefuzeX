"""Explicit local observation independent of evaluation, accounts and transport."""

from __future__ import annotations

import copy
import os
import re
import threading
import time
import uuid
import warnings
from collections.abc import Callable
from contextlib import suppress
from contextvars import copy_context
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from ._version import __version__
from .errors import ConfigurationError, ValidationError
from .evidence.observation_capture import ObservationBuffer
from .evidence.observation_otel import ACTIVE_OBSERVATION, attach, span_key
from .evidence.trace import TraceEvidenceLimits
from .repository.privacy import scan_sensitive_text
from .trajectory_privacy import checked_policy, privacy_operation

_P = ParamSpec("_P")
_R = TypeVar("_R")


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

    @privacy_operation(
        "Original content is retained in local observation memory; an explicit save creates plaintext."
    )
    def __init__(
        self,
        *,
        tracer_provider: Any = None,
        external_run_id: str | None = None,
        external_invocation_id: str | None = None,
        limits: TraceEvidenceLimits | None = None,
        redact_sensitive: bool = True,
    ) -> None:
        """Prepare an inactive session; arguments and guarantees match ``observe``.

        Construction validates only local options and creates an opaque UUID;
        provider attachment is deferred until entry. No files or keys are read.

        Args:
            redact_sensitive: Strict bool, default True. False explicitly retains original
                bounded content for this operation only; existing redaction cannot be undone.
        """
        self._redact_sensitive = checked_policy(redact_sensitive)
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
        self._buffer = ObservationBuffer(chosen, redact_sensitive=redact_sensitive)
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
        With a compatible provider but no completed spans, emit a nonfatal
        RuntimeWarning; warning filters cannot replace the Agent outcome. The
        existing status/reasons do not infer unseen thread activity or its count.
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
            if not self._redact_sensitive:
                self._result["content_privacy"] = "raw"
        if self._processor is not None and self._buffer.observed == 0:
            # Warning filters/handlers cannot replace the Agent outcome; the
            # export retains its existing gap status even if display fails.
            with suppress(Exception):
                warnings.warn(
                    "KUMA captured no completed spans. Check instrumentation and use "
                    "session.bind(worker) for worker threads or server callbacks; "
                    "join workers before leaving the session. Saved data does not "
                    "prove capture succeeded; inspect capture_status and reasons.",
                    RuntimeWarning,
                    stacklevel=2,
                )
        return False

    def bind(self, callback: Callable[_P, _R]) -> Callable[_P, _R]:
        """Bind synchronous work explicitly to this observation's capture window.

        Args:
            callback: Required synchronous callable. Its positional/keyword
                arguments and return value pass through unchanged. Do not pass
                coroutine functions or callbacks returning deferred async work.
        Returns:
            Reusable callable with the same signature. Each invocation runs in
            a fresh copy of the binding-time context, including OTel parentage;
            concurrent calls never enter the same Context object.
        Raises:
            ConfigurationError: Binding or invoking outside the active window,
                or callback is not callable. Callback exceptions propagate intact.
        Preconditions:
            Enter this session before binding. Join workers before session exit;
            this helper does not start threads or own servers/executors.
        Postconditions:
            Caller context is restored on return, exception or cancellation.
            Late invocations fail before executing the callback. Work already
            running when the window closes is not reassigned to another session.
        Side Effects:
            Snapshots context variables at binding, without invoking the callback.
            Invocation temporarily selects this session even inside another one.
        Security/Privacy:
            Only explicitly bound work is attributed. No global thread inference,
            uploads or changes to user exporters; normal capture redaction applies.
        """
        with self._lock:
            if self._state != "active" or not callable(callback):
                raise ConfigurationError("Bind a callable inside an active session")
            context = copy_context()
            context.run(ACTIVE_OBSERVATION.set, self)

        @wraps(callback)
        def bound(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            """Reject late work and isolate each caller's context and exceptions."""
            with self._lock:
                if self._state != "active":
                    raise ConfigurationError("The bound observation session is closed")
            return context.copy().run(callback, *args, **kwargs)

        return bound

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

    @privacy_operation(
        "Original content is returned as local data; saving it creates plaintext."
    )
    def export(self, *, redact_sensitive: bool = True) -> dict[str, Any]:
        """Return detached recorded JSON after exit, requiring explicit opt-out for raw data.

        Returns:
            Closed ``kuma.observation.v1`` document, at most 5 MiB canonical JSON.
            No Case, verdict, report locator, cloud state or charge is invented.
        Raises:
            ConfigurationError: The context has not finished.
        Postconditions:
            Mutating the result cannot change stored capture or subsequent exports.

        Args:
            redact_sensitive: Strict bool, default True. False explicitly retains original
                bounded content for this operation only; existing redaction cannot be undone.
        """
        with self._lock:
            if self._result is None:
                raise ConfigurationError("Finish the observation before exporting it")
            if not self._redact_sensitive and redact_sensitive:
                raise ValidationError(
                    "Raw observation requires explicit redact_sensitive=False"
                )
            return copy.deepcopy(self._result)

    def render_text(self) -> str:
        """Return a bounded human-readable timeline of actually captured spans.

        Shows safe span name, operation, duration and status, not raw body text.
        Times are recorded span duration, not inferred queue/model billing time.
        Missing capture is unknown, not proof that no tools ran. Requires exit.
        """
        from .capture_privacy import redact_capture_json

        with self._lock:
            if self._result is None:
                raise ConfigurationError("Finish the observation before rendering it")
            data, _ = redact_capture_json(self._result)
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

    @privacy_operation("Original observation content will be saved as local plaintext.")
    def save(
        self,
        path: str | os.PathLike[str],
        *,
        root: str | os.PathLike[str],
        redact_sensitive: bool = True,
    ) -> Path:
        """Atomically save the finished export in an explicitly authorized directory.

        Args:
            path: New JSON filename, relative to root or absolute inside it.
                Existing files are never overwritten; parent must already exist.
            root: Existing local directory authorizing writes. Links, mount escapes
                and reparse paths fail closed; no repository scanning occurs.
            redact_sensitive: Strict bool, default True. False explicitly retains original
                bounded content for this operation only; existing redaction cannot be undone.
        Returns:
            Absolute published path containing exactly ``export()`` as JSON.
        Raises:
            ConfigurationError: Session unfinished, unsafe path or local I/O failure.
        Side Effects:
            Write/fsync a temporary sibling, atomically publish and clean temporary
            resources. Failure does not alter existing files. No network or upload.
        """
        from .repository.observation_io import save_observation

        return save_observation(
            self.export(redact_sensitive=redact_sensitive), path, root=root
        )


@privacy_operation(
    "Original content is retained in local observation memory; an explicit save creates plaintext."
)
def observe(
    *,
    tracer_provider: Any = None,
    external_run_id: str | None = None,
    external_invocation_id: str | None = None,
    limits: TraceEvidenceLimits | None = None,
    redact_sensitive: bool = True,
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
        redact_sensitive: Strict bool, default True. False explicitly retains original
            bounded content for this operation only; existing redaction cannot be undone.
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
        redact_sensitive=redact_sensitive,
    )
