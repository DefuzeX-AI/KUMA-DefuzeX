"""Lazy OTel attachment and context-local routing; never flush user exporters."""

from __future__ import annotations

import threading
import weakref
from contextvars import ContextVar
from typing import Any

from .trace import _span_key

ACTIVE_OBSERVATION: ContextVar[Any] = ContextVar("kuma_observation", default=None)
_ATTACH_LOCK = threading.RLock()
_PROCESSORS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class ObservationProcessor:
    """Route actual start/end callbacks without retaining sessions or providers.

    A provider owns one inert reusable processor. OTel has no public detach API;
    closing a session removes its registrations, not the user-owned processor.
    ContextVars isolate threads/tasks and nested scopes; end callbacks may arrive
    on another thread. Registry entries only weakly reference live sessions.
    """

    def __init__(self) -> None:
        """Initialize bounded per-session registration routing and attachment state."""
        self.sessions: weakref.WeakSet = weakref.WeakSet()
        self.lock = threading.RLock()
        self.available = False

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        """Register only the innermost matching active session; never infer owners."""
        session = ACTIVE_OBSERVATION.get()
        if session is not None and session._processor is self:
            session._start(span)

    def _on_ending(self, span: Any) -> None:
        """Provide the optional recent OTel callback without touching user spans."""

    def on_end(self, span: Any) -> None:
        """Route ended spans by recorded identity, including cross-thread endings."""
        with self.lock:
            sessions = list(self.sessions)
        for session in sessions:
            session._end(span)

    def shutdown(self) -> None:
        """Leave user provider lifecycle ownership unchanged; no resources to close."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Report synchronous callback delivery; do not flush other processors."""
        return True


def attach(provider: Any) -> ObservationProcessor | None:
    """Attach at most once to a compatible weak-referenceable provider.

    None resolves the current global provider lazily; absent OTel/proxy/failure
    returns None. Cache before invoking add to avoid repeated partial side
    effects from a provider that attaches and then raises. No provider is created,
    configured, flushed or shut down, and raw exceptions never escape.
    """
    try:
        if provider is None:
            from opentelemetry.trace import get_tracer_provider

            provider = get_tracer_provider()
        add = getattr(provider, "add_span_processor", None)
        if not callable(add):
            return None
        with _ATTACH_LOCK:
            processor = _PROCESSORS.get(provider)
            if processor is None:
                processor = ObservationProcessor()
                _PROCESSORS[provider] = processor
                add(processor)
                processor.available = True
            return processor if processor.available else None
    except Exception:
        return None


def span_key(span: Any) -> tuple[int, int] | None:
    """Read only actual OTel identity; faulty accessors remain safely unavailable."""
    try:
        return _span_key(span)
    except Exception:
        return None
