"""Identity-free, bounded span accounting for explicitly opted-in observation."""

from __future__ import annotations

import copy
from typing import Any

from .trace import _capture_summary, _span_priority, _topology_closed
from .trace_mapping import SpanMappingError, json_size, map_span
from .trace_model_content import omit_largest_model_body

MAX_OBSERVATION_BYTES = 5 * 1024 * 1024


class ObservationBuffer:
    """Own normalized telemetry only; the session serializes access with its lock.

    Reuse Trace mapping, sampling priority, topology and summary contracts without
    constructing a Run, Case or synthetic execution identity. Raw span objects
    are never retained. Counters are capped at the existing wire maximum.
    """

    def __init__(self, limits: Any) -> None:
        """Initialize empty capture bounded by validated TraceEvidenceLimits."""
        self.limits = limits
        self.spans: list[dict[str, Any]] = []
        self.observed = 0
        self.fields = 0
        self.reasons: set[str] = set()
        self.truncated = False

    def loss(self, reason: str, *, count: int = 0) -> None:
        """Record unavailable spans and a stable mapper/lifecycle reason."""
        self.observed = min(999_999_999, self.observed + count)
        self.reasons.add(reason)
        self.truncated = True

    def add(self, span: Any) -> None:
        """Map one actual ended span; failures never retain provider error text.

        Called inside the session lock. Mapping uses the canonical redactor and
        allowlist. Sampling retains higher-priority boundaries, then budget
        fitting omits optional model bodies before whole spans.
        """
        self.observed = min(999_999_999, self.observed + 1)
        try:
            mapped, drops, truncated, reasons = map_span(span, self.limits)
        except SpanMappingError as error:
            self.loss(error.reason)
            return
        except Exception:
            self.loss("trace_serialization_failed")
            return
        self.fields = min(999_999_999, self.fields + drops)
        self.reasons.update(reasons)
        self.truncated |= truncated
        identity = (mapped["trace_id"], mapped["span_id"])
        if any((s["trace_id"], s["span_id"]) == identity for s in self.spans):
            self.loss("trace_span_duplicate")
            return
        self.spans.append(mapped)
        if len(self.spans) > min(self.limits.max_spans, 10_000):
            self._remove_low_priority()
            self.loss("trace_span_limit")
            self.reasons.add("trace_span_sampled")
        while json_size(self.spans) > min(
            self.limits.max_total_bytes, MAX_OBSERVATION_BYTES
        ):
            self._reduce()

    def _remove_low_priority(self) -> None:
        """Reuse Trace sampling rank and avoid removing retained parents first."""
        parents = {s["parent_span_id"] for s in self.spans}
        worst = max(self.spans, key=lambda span: _span_priority(span, parents))
        self.spans.remove(worst)

    def _reduce(self) -> None:
        """Reduce a nonempty over-budget buffer by one whole field or span.

        Both callers first prove that serialized content exceeds its positive
        budget; bounded metadata alone fits the minimum observation budget.
        """
        if omit_largest_model_body(self.spans):
            self.fields = min(999_999_999, self.fields + 1)
            self.reasons.add("trace_model_content_size_limit")
        else:
            self._remove_low_priority()
            self.loss("trace_byte_limit")

    def finish(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Close topology and bound the entire export, including JSON overhead.

        The caller has already stopped registration. Return detached plain JSON;
        no network, flush, persistence or fake evaluation is performed. Empty
        capture is explicitly unavailable rather than proof of no tool activity.
        """
        if not self.spans:
            self.reasons.add("trace_capture_failed")
        while True:
            closed, lost = _topology_closed(self.spans)
            self.spans = list(closed)
            if lost:
                self.loss("trace_topology_partial")
            result = self._export(metadata)
            if json_size(result) <= min(
                MAX_OBSERVATION_BYTES, self.limits.max_total_bytes
            ):
                return copy.deepcopy(result)
            self._reduce()

    def _export(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Build the shared capture accounting around identity-free metadata."""
        reasons = sorted(self.reasons)
        dropped = min(999_999_999, self.observed - len(self.spans) + self.fields)
        degraded = bool(reasons or dropped or self.truncated)
        return {
            **metadata,
            "spans": sorted(
                self.spans,
                key=lambda s: (s["start_time_unix_nano"], s["trace_id"], s["span_id"]),
            ),
            "capture_status": (
                ("partial" if self.spans else "failed") if degraded else "complete"
            ),
            "capture_summary": _capture_summary(
                observed_spans=self.observed,
                retained_spans=len(self.spans),
                dropped_attribute_events=self.fields,
                topology_complete="trace_topology_partial" not in reasons,
                observed_logs=0,
                retained_logs=0,
                dropped_log_fields=0,
            ),
            "reasons": reasons,
            "dropped_count": dropped,
            "truncated": self.truncated,
        }
