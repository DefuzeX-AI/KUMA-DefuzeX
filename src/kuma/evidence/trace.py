"""Bounded, transactional in-process Trace Evidence capture."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from ..contracts import CaptureComponent
from ..errors import ConfigurationError
from .otel_log_mapping import (
    LogRecordMappingError,
    build_log_segment,
    log_record_sort_key,
    map_log_record,
)
from .runtime import runtime_submission_id
from .trace_mapping import SpanMappingError, extract_agent_output, json_size, map_span

_CAPTURE_REASONS = frozenset(
    {
        "trace_attribute_filtered",
        "trace_attribute_invalid",
        "trace_attribute_limit",
        "trace_attribute_not_allowlisted",
        "trace_budget_exhausted",
        "trace_byte_limit",
        "trace_capture_failed",
        "trace_drop_count_saturated",
        "trace_event_limit",
        "trace_event_invalid",
        "trace_export_failed",
        "trace_flush_failed",
        "trace_serialization_failed",
        "trace_span_duplicate",
        "trace_parent_invalid",
        "trace_scope_invalid",
        "trace_span_context_invalid",
        "trace_span_limit",
        "trace_span_outside_window",
        "trace_span_sampled",
        "trace_span_timing_invalid",
        "trace_topology_partial",
        "trace_value_invalid",
        "trace_value_truncated",
    }
)
_MAX_DROPPED_COUNT = 999_999_999


def _evidence_payload(
    run_id: str,
    case_id: str,
    input_id: str,
    *,
    spans: list[Mapping[str, Any]] | None = None,
    dropped_count: int = 0,
    truncated: bool = False,
    reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build one versioned Trace envelope with stable association and accounting."""
    return {
        "schema_version": "defuzex.trace_evidence.v1",
        "run_id": run_id,
        "case_id": case_id,
        "input_id": input_id,
        "spans": [] if spans is None else spans,
        "dropped_count": dropped_count,
        "truncated": truncated,
        "reasons": list(reasons),
    }


_MIN_TRACE_EVIDENCE_BYTES = json_size(_evidence_payload("r", "c", "i"))


@dataclass(frozen=True, slots=True)
class TraceEvidenceLimits:
    """Set hard in-memory and serialized limits for in-process OTel capture.

    Args:
        max_spans: Maximum retained ended spans for one active step before
            deterministic topology-aware sampling drops records.
        max_attributes: Maximum safe allowlisted attributes inspected/retained
            per span or resource projection.
        max_events_per_span: Maximum OTel events retained per span.
        max_text_length: Maximum characters retained for each allowlisted name or
            metadata value before truncation.
        max_total_bytes: Maximum UTF-8 bytes of complete Trace Evidence envelopes
            committed across one Run, including envelope and reason overhead.
        max_log_records: Maximum normalized OTel log records retained per step.
        max_log_bytes: Maximum UTF-8 bytes of structured OTel log artifacts
            committed across one Run.

    Raises:
        ConfigurationError: If any limit is a boolean, is not a positive
            integer, or ``max_total_bytes`` cannot hold the smallest valid Trace
            Evidence envelope.

    Preconditions:
        Choose limits according to the maximum local memory and Evidence size
        the application is prepared to retain. Increasing a number does not
        enable additional attribute or log-body fields.

    Postconditions:
        A valid instance provides immutable budgets used by
        :class:`TraceEvidenceCapture` for every step in one Run. Excluded,
        sampled, or truncated telemetry is reported through stable capture
        status and reason fields instead of exceeding these budgets.

    Security/Privacy:
        These values only reduce or enlarge resource budgets. They never widen
        the Trace/OTel-log allowlists, permit raw prompt or log bodies, or bypass
        KUMA's sensitive-data policy.
    """

    max_spans: int = 200
    max_attributes: int = 32
    max_events_per_span: int = 20
    max_text_length: int = 256
    max_total_bytes: int = 8 * 1024 * 1024
    max_log_records: int = 200
    max_log_bytes: int = 128_000

    def __post_init__(self) -> None:
        """Reject limits that cannot bound a minimally valid Trace envelope."""
        for name in (
            "max_spans",
            "max_attributes",
            "max_events_per_span",
            "max_text_length",
            "max_total_bytes",
            "max_log_records",
            "max_log_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigurationError(f"{name} must be a positive integer")
        if self.max_total_bytes < _MIN_TRACE_EVIDENCE_BYTES:
            raise ConfigurationError(
                "max_total_bytes is smaller than the minimum Trace Evidence envelope "
                f"({_MIN_TRACE_EVIDENCE_BYTES} bytes)"
            )


@dataclass(slots=True)
class _Association:
    """Correlate exported telemetry with the one currently active Run step.

    Attributes:
        run_id: Owning public Run identifier.
        case_id: Owning public Case identifier.
        input_id: Current step/input identifier.
        active: Whether late exports may still enter this step bucket.
    """

    run_id: str
    case_id: str
    input_id: str
    active: bool = True


@dataclass(slots=True)
class _Bucket:
    """Hold bounded mutable telemetry awaiting one step transaction.

    Attributes:
        spans: Retained ``(sequence, mapped_span, encoded_size)`` records.
        next_sequence: Monotonic capture sequence for deterministic ordering.
        bytes_used: Approximate bytes currently held by mapped spans.
        dropped_count: Span/event observations omitted from retained Evidence.
        truncated: Whether any span-side limit or mapping loss occurred.
        reasons: Stable span-side degradation reason codes.
        agent_output: Best supported final Agent output with ordering priority.
        observed_spans: Total associated spans seen before sampling.
        dropped_attribute_events: Attribute/event fields removed by privacy or limits.
        log_records: Retained normalized OTel log records with sequence and size.
        next_log_sequence: Monotonic capture sequence for log ordering.
        log_bytes_used: Bytes currently held by normalized log records.
        observed_logs: Total associated OTel log records observed.
        dropped_logs: Log records omitted by mapping or limits.
        dropped_log_fields: Individual log fields filtered or truncated.
        log_reasons: Stable log-side degradation reason codes.
    """

    spans: list[tuple[int, Mapping[str, Any], int]] = field(default_factory=list)
    next_sequence: int = 0
    bytes_used: int = 0
    dropped_count: int = 0
    truncated: bool = False
    reasons: set[str] = field(default_factory=set)
    agent_output: tuple[tuple[int, int, int], Any] | None = None
    observed_spans: int = 0
    dropped_attribute_events: int = 0
    log_records: list[tuple[int, Mapping[str, Any], int]] = field(default_factory=list)
    next_log_sequence: int = 0
    log_bytes_used: int = 0
    observed_logs: int = 0
    dropped_logs: int = 0
    dropped_log_fields: int = 0
    log_reasons: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _TraceSnapshot:
    """Freeze one bucket view while building transactional Evidence.

    Attributes:
        sequence: Bucket sequence boundary included by this snapshot.
        spans: Deterministically ordered retained span mappings.
        reasons: Stable span degradation reasons.
        dropped: Total omitted span observations.
        truncated: Whether span Evidence is incomplete.
        observed_spans: Total spans observed before sampling.
        dropped_attribute_events: Filtered/truncated span fields count.
        log_records: Deterministically ordered normalized log records.
        observed_logs: Total OTel log records observed.
        dropped_logs: Omitted log-record count.
        dropped_log_fields: Filtered/truncated log-field count.
        log_reasons: Stable log degradation reasons.
    """

    sequence: int
    spans: list[Mapping[str, Any]]
    reasons: tuple[str, ...]
    dropped: int
    truncated: bool
    observed_spans: int
    dropped_attribute_events: int
    log_records: list[Mapping[str, Any]]
    observed_logs: int
    dropped_logs: int
    dropped_log_fields: int
    log_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedOtelLogs:
    """Stage safe OTel log segments in the owning Submission transaction.

    Attributes:
        segments: Structured bounded log artifacts ready for Submission logs.
        component: Public completeness status for OTel log capture.
        missing: Stable missing/degradation reason codes.
        dropped_count: Omitted records and fields counted for the Submission.
        observed_count: Associated OTel records seen before filtering.
        retained_count: Records represented in ``segments``.
        _encoded_size: Run byte budget reserved if the transaction commits.
    """

    segments: tuple[Mapping[str, Any], ...] = ()
    component: CaptureComponent = field(default_factory=CaptureComponent)
    missing: tuple[str, ...] = ()
    dropped_count: int = 0
    observed_count: int = 0
    retained_count: int = 0
    _encoded_size: int = 0


@dataclass(slots=True)
class PreparedTraceEvidence:
    """Stage Trace/OTel Evidence until the enclosing Submission commits.

    Attributes:
        evidence: Versioned Trace Evidence mapping, or ``None`` when unavailable.
        component: Public Trace capture completeness status.
        missing: Stable reasons for absent or partial Trace Evidence.
        dropped_count: Omitted telemetry observations charged to the Submission.
        capture_summary: Safe aggregate counts/topology status for diagnostics.
        otel_logs: OTel logs staged in the same step transaction.
        _capture: Capture owner that commits/aborts the reservation.
        _association: Step association, or ``None`` for a no-capture preparation.
        _sequence: Snapshot boundary protected from late telemetry.
        _encoded_size: Trace bytes reserved against the Run budget.
        _finished: Whether commit or abort already finalized this preparation.
    """

    evidence: Mapping[str, Any] | None
    component: CaptureComponent
    missing: tuple[str, ...]
    dropped_count: int
    capture_summary: Mapping[str, Any] | None
    otel_logs: PreparedOtelLogs
    _capture: TraceEvidenceCapture
    _association: _Association | None
    _sequence: int
    _encoded_size: int = 0
    _finished: bool = False

    def commit(self) -> None:
        """Commit prepared capture state only after the enclosing submission succeeds."""
        if self._finished:
            return
        if self._association is not None:
            self._capture._commit(
                self._association,
                self._sequence,
                self._encoded_size,
                self.otel_logs._encoded_size,
            )
        self._finished = True

    def abort(self) -> None:
        """Discard prepared capture state after a failed enclosing submission."""
        if self._finished:
            return
        if self._association is not None:
            self._capture._abort(self._association)
        self._finished = True


class TraceEvidenceCapture:
    """Collect ended in-process OTel telemetry for one active Run step at a time.

    The capture is normally returned by ``configure_trace_evidence`` or attached
    automatically by ``create_run`` when a compatible global provider exists.
    It is thread-safe and associates worker-thread spans at span start, but it
    does not receive spans from another process or an OTLP network exporter.

    Security/Privacy:
        Mapping retains a strict allowlist of identifiers, timing, status, safe
        resource/scope metadata, and approved ``gen_ai`` usage/model attributes.
        Arbitrary attributes, prompt/completion text, raw logs, tool arguments,
        source, and credentials are filtered or represented only by safe hashes.
    """

    def __init__(self, limits: TraceEvidenceLimits | None = None) -> None:
        """Initialize empty thread-safe capture with bounded Run budgets.

        Args:
            limits: Resource limits to enforce, or ``None`` for safe defaults.

        Raises:
            ConfigurationError: If the supplied limits cannot represent even a
                minimal valid Trace Evidence envelope.

        Postconditions:
            No Run/step is active, no telemetry is retained, and byte accounting
            starts at zero. No provider has been attached by this constructor.

        Side Effects:
            Allocates local locks/maps only; performs no network or filesystem I/O.
        """
        self.limits = limits or TraceEvidenceLimits()
        self._lock = threading.RLock()
        self._buckets: dict[int, _Bucket] = {}
        self._run_bytes: dict[tuple[str, str], int] = {}
        self._run_log_bytes: dict[tuple[str, str], int] = {}
        self._active: _Association | None = None
        self._registrations: dict[tuple[int, int], _Association] = {}
        self._prewindow_registrations: set[tuple[int, int]] = set()
        self._force_flush: Callable[[], bool] | None = None

    def _set_force_flush(self, callback: Callable[[], bool]) -> None:
        """Register the attached provider's flush callback for step finalization."""
        self._force_flush = callback

    def begin_step(self, run_id: str, case_id: str, input_id: str) -> None:
        """Open the telemetry transaction for exactly one delivered Run input.

        Args:
            run_id: Non-empty owning Run identifier.
            case_id: Non-empty owning Case identifier.
            input_id: Non-empty current input/step identifier.

        Raises:
            ConfigurationError: If identifiers are invalid or their minimal
                envelope alone exceeds the Run byte budget.
            RuntimeError: If a prior step is still active. The conflicting prior
                bucket is discarded to prevent cross-step leakage.

        Preconditions:
            The previous step was committed, aborted, cancelled, or the Run was
            finished. ``Run.get_input`` owns this lifecycle call.

        Postconditions:
            Success installs one empty active bucket; subsequent same-process
            span starts and log exports correlate to these identifiers.

        Side Effects:
            Changes only in-memory association state.
        """
        if any(
            not isinstance(value, str) or not value
            for value in (run_id, case_id, input_id)
        ):
            raise ConfigurationError(
                "Trace Evidence Run, Case, and Input IDs must be non-empty text"
            )
        envelope_size = json_size(_evidence_payload(run_id, case_id, input_id))
        if envelope_size > self.limits.max_total_bytes:
            raise ConfigurationError(
                "Trace Evidence identifiers exceed max_total_bytes before capture"
            )
        association = _Association(run_id, case_id, input_id)
        with self._lock:
            if self._active is not None and self._active.active:
                self._discard_locked(self._active)
                raise RuntimeError(
                    "Trace Evidence is already associated with a Run step"
                )
            self._buckets[id(association)] = _Bucket()
            self._active = association

    def register_span(self, span: Any) -> None:
        """Associate a span that starts inside the current step capture window.

        The window opens in :meth:`begin_step` and closes when
        :meth:`prepare_step` snapshots the Submission. A valid span started
        before or after that window is not registered; if it later ends while a
        step is active, :meth:`export_registered_span` accounts for the loss.
        This method performs no network or filesystem I/O.
        """
        key = _span_key(span)
        with self._lock:
            association = self._active
            if key is None:
                return
            if association is None or not association.active:
                if len(self._prewindow_registrations) < self.limits.max_spans * 4:
                    self._prewindow_registrations.add(key)
                return
            bucket = self._buckets.get(id(association))
            if bucket is None or key in self._registrations:
                return
            registered = sum(
                candidate is association for candidate in self._registrations.values()
            )
            if registered >= self.limits.max_spans * 4:
                bucket.observed_spans += 1
                self._note_sampled_span(bucket)
                return
            self._registrations[key] = association

    def export_registered_span(self, span: Any) -> None:
        """Map an ended span or account for an end inside the wrong window.

        Spans registered at start retain their original step association across
        worker threads. An unregistered span that ends while a step is active is
        counted as ``trace_span_outside_window`` rather than disappearing. A
        span ending when no step is active cannot be safely attributed and is
        ignored to prevent cross-step or cross-Run leakage.
        """
        key = _span_key(span)
        if key is None:
            return
        with self._lock:
            association = self._registrations.pop(key, None)
            if association is None:
                started_before_window = key in self._prewindow_registrations
                self._prewindow_registrations.discard(key)
                active = self._active
                if started_before_window and active is not None and active.active:
                    bucket = self._buckets.get(id(active))
                    if bucket is not None:
                        bucket.observed_spans += 1
                        self._note_drop(
                            bucket,
                            1,
                            "trace_span_outside_window",
                            truncated=True,
                        )
                return
        self.export_span(span, association=association)

    def export_span(
        self, span: Any, *, association: _Association | None = None
    ) -> None:
        """Map an ended span into the currently active same-process Run step."""
        if association is None:
            with self._lock:
                association = self._active
        if association is None or not association.active:
            return
        with self._lock:
            bucket = self._buckets.get(id(association))
            if bucket is None or not association.active:
                return
            bucket.observed_spans += 1
        try:
            mapped, dropped, truncated, reasons = map_span(span, self.limits)
            size = json_size(mapped)
            agent_output = extract_agent_output(span, self.limits.max_total_bytes)
        except SpanMappingError as exc:
            self.record_failure(exc.reason, association=association)
            return
        except Exception:
            self.record_failure("trace_serialization_failed", association=association)
            return
        with self._lock:
            bucket = self._buckets.get(id(association))
            if bucket is None or not association.active:
                return
            if agent_output is not None:
                rank, output = agent_output
                if bucket.agent_output is None or rank >= bucket.agent_output[0]:
                    bucket.agent_output = (rank, output)
            self._increment_dropped(bucket, dropped)
            bucket.dropped_attribute_events += dropped
            bucket.truncated = bucket.truncated or truncated
            bucket.reasons.update(
                reason for reason in reasons if reason in _CAPTURE_REASONS
            )
            self._retain_sampled_span(bucket, association, mapped, size)

    def export_log_record(self, record: Any) -> None:
        """Capture one ended OTel LogRecord without retaining its raw values."""

        with self._lock:
            association = self._active
            if association is None or not association.active:
                return
            bucket = self._buckets.get(id(association))
            if bucket is None:
                return
            bucket.observed_logs += 1
        try:
            mapped, dropped, _, reasons = map_log_record(record, self.limits)
            size = json_size(mapped)
        except LogRecordMappingError as exc:
            self.record_log_failure(exc.reason, association=association)
            return
        except Exception:
            self.record_log_failure("otel_log_invalid", association=association)
            return
        with self._lock:
            bucket = self._buckets.get(id(association))
            if bucket is None or not association.active:
                return
            bucket.dropped_log_fields = min(
                bucket.dropped_log_fields + dropped, _MAX_DROPPED_COUNT
            )
            bucket.log_reasons.update(reasons)
            if len(bucket.log_records) >= self.limits.max_log_records:
                self._drop_log(bucket, "otel_log_limit")
                return
            if bucket.log_bytes_used + size > self.limits.max_log_bytes:
                self._drop_log(bucket, "otel_log_byte_limit")
                return
            sequence = bucket.next_log_sequence
            bucket.next_log_sequence += 1
            bucket.log_records.append((sequence, mapped, size))
            bucket.log_bytes_used += size

    def _retain_sampled_span(
        self,
        bucket: _Bucket,
        association: _Association,
        mapped: Mapping[str, Any],
        size: int,
    ) -> None:
        """Insert or replace a sampled span while maintaining topology and byte counts."""
        identity = (mapped["trace_id"], mapped["span_id"])
        if any(
            (item[1]["trace_id"], item[1]["span_id"]) == identity
            for item in bucket.spans
        ):
            self._note_drop(bucket, 1, "trace_span_duplicate", truncated=True)
            return
        replacement = self._sample_replacement(bucket, mapped)
        if len(bucket.spans) >= self.limits.max_spans and replacement is None:
            self._note_sampled_span(bucket)
            return
        replaced_size = 0 if replacement is None else bucket.spans[replacement][2]
        run_bytes = self._run_bytes.get((association.run_id, association.case_id), 0)
        projected = run_bytes + bucket.bytes_used - replaced_size + size
        if projected > self.limits.max_total_bytes:
            self._note_drop(bucket, 1, "trace_byte_limit", truncated=True)
            return
        sequence = bucket.next_sequence
        bucket.next_sequence += 1
        item = (sequence, mapped, size)
        if replacement is None:
            bucket.spans.append(item)
        else:
            bucket.spans[replacement] = item
            self._note_sampled_span(bucket)
        bucket.bytes_used += size - replaced_size

    def _sample_replacement(
        self, bucket: _Bucket, candidate: Mapping[str, Any]
    ) -> int | None:
        """Choose a deterministic lower-priority span eligible for replacement."""
        if len(bucket.spans) < self.limits.max_spans:
            return None
        protected = {
            span["parent_span_id"]
            for _, span, _ in bucket.spans
            if span["parent_span_id"] is not None
        }
        if candidate["parent_span_id"] is not None:
            protected.add(candidate["parent_span_id"])
        priorities = [
            (_span_priority(span, protected), index)
            for index, (_, span, _) in enumerate(bucket.spans)
            if span["span_id"] not in protected
        ]
        if not priorities:
            return None
        worst_priority, worst_index = max(priorities)
        if _span_priority(candidate, protected) >= worst_priority:
            return None
        return worst_index

    def _note_sampled_span(self, bucket: _Bucket) -> None:
        """Mark deterministic sampling and increment the dropped-span accounting."""
        self._note_drop(bucket, 1, "trace_span_limit", truncated=True)
        bucket.reasons.add("trace_span_sampled")

    def output_for_step(self, run_id: str, case_id: str, input_id: str) -> Any | None:
        """Return the preferred standard OTel final output for this step.

        A valid ``invoke_workflow`` result outranks every ``invoke_agent`` result
        because it represents the orchestration's final answer. Within the same
        operation class, the latest end time wins and span ID breaks exact ties.
        The value is read only from bounded semantic-convention fields and is not
        copied into Trace Evidence.
        """

        association = self._matching_association(run_id, case_id, input_id)
        self._flush(association)
        with self._lock:
            bucket = self._buckets.get(id(association))
            if bucket is None or bucket.agent_output is None:
                return None
            return bucket.agent_output[1]

    def record_failure(
        self, reason: str, *, association: _Association | None = None
    ) -> None:
        """Record a bounded stable span-capture reason without exception text."""
        with self._lock:
            target = association or self._active
            if target is None or not target.active:
                return
            bucket = self._buckets.get(id(target))
            if bucket is not None:
                bounded_reason = (
                    reason if reason in _CAPTURE_REASONS else "trace_capture_failed"
                )
                self._note_drop(bucket, 1, bounded_reason)

    def record_log_failure(
        self, reason: str, *, association: _Association | None = None
    ) -> None:
        """Record a bounded stable OTel Logs reason without raw record content."""
        with self._lock:
            target = association or self._active
            if target is None or not target.active:
                return
            bucket = self._buckets.get(id(target))
            if bucket is not None:
                self._drop_log(bucket, reason)

    def prepare_step(
        self, run_id: str, case_id: str, input_id: str
    ) -> PreparedTraceEvidence:
        """Stage one associated step's flushed OTel Trace and Logs Evidence.

        The active provider is flushed before a lock-protected snapshot is fit
        to the remaining per-Run Trace and log budgets. The returned
        ``PreparedTraceEvidence`` is coupled to the current association and
        sequence: ``commit()`` charges retained bytes and seals the step only
        after Submission success, while ``abort()`` discards it without charging
        the Run so retry and late telemetry cannot leak into another step.

        Args:
            run_id: Expected active Run identifier.
            case_id: Expected active Case identifier.
            input_id: Expected active input/step identifier.

        Returns:
            Transactional :class:`PreparedTraceEvidence` containing bounded
            Trace Evidence, mapped OTel logs, status, and accounting.

        Raises:
            RuntimeError: If identifiers do not match the active association.
            ConfigurationError: If a minimal valid result cannot fit configured
                limits.

        Preconditions:
            ``begin_step`` opened the exact association and the Agent has ended
            all telemetry it expects this Submission to include.

        Postconditions:
            Success snapshots a fixed sequence but does not charge Run budgets or
            close the association. ``commit`` seals/charges it; ``abort`` discards
            it. Flush/mapping failures are represented as partial/missing status.

        Side Effects:
            Requests provider ``force_flush`` and reads in-memory telemetry only.

        Security/Privacy:
            Only allowlisted mapped values and hash-only log content are returned;
            exporter exceptions and raw telemetry values are not exposed.
        """
        association = self._matching_association(run_id, case_id, input_id)
        self._flush(association)
        snapshot = self._snapshot(association)
        evidence, reasons, dropped, encoded_size = self._fit_evidence(
            association,
            snapshot.spans,
            snapshot.reasons,
            snapshot.dropped,
            snapshot.truncated,
        )
        otel_logs = self._prepare_otel_logs(association, snapshot)
        status = "failed" if evidence is None else "complete"
        if evidence is not None and (
            reasons or dropped > 0 or bool(evidence.get("truncated"))
        ):
            status = "partial" if evidence["spans"] else "failed"
        retained_spans = 0 if evidence is None else len(evidence["spans"])
        return PreparedTraceEvidence(
            evidence=evidence,
            component=CaptureComponent(status=status, reasons=reasons),
            missing=tuple(f"trace_evidence:{reason}" for reason in reasons),
            dropped_count=dropped,
            capture_summary=_capture_summary(
                observed_spans=snapshot.observed_spans,
                retained_spans=retained_spans,
                dropped_attribute_events=snapshot.dropped_attribute_events,
                topology_complete="trace_topology_partial" not in reasons,
                observed_logs=otel_logs.observed_count,
                retained_logs=otel_logs.retained_count,
                dropped_log_fields=snapshot.dropped_log_fields,
            ),
            otel_logs=otel_logs,
            _capture=self,
            _association=association,
            _sequence=snapshot.sequence,
            _encoded_size=encoded_size,
        )

    def _prepare_otel_logs(
        self, association: _Association, snapshot: _TraceSnapshot
    ) -> PreparedOtelLogs:
        """Build a bounded associated OTel Logs segment from captured records."""
        if snapshot.observed_logs == 0 and not snapshot.log_reasons:
            return PreparedOtelLogs()
        run_key = (association.run_id, association.case_id)
        with self._lock:
            available = self.limits.max_log_bytes - self._run_log_bytes.get(run_key, 0)
        built = build_log_segment(
            run_id=association.run_id,
            input_id=association.input_id,
            submission_id=runtime_submission_id(
                association.run_id, association.input_id
            ),
            records=snapshot.log_records,
            observed_count=snapshot.observed_logs,
            dropped_count=snapshot.dropped_logs,
            reasons=set(snapshot.log_reasons),
            max_bytes=max(0, available),
        )
        status = "complete"
        if built.reasons:
            status = "partial" if built.segment is not None else "failed"
        return PreparedOtelLogs(
            segments=() if built.segment is None else (built.segment,),
            component=CaptureComponent(status=status, reasons=built.reasons),
            missing=tuple(f"otel_logs:{reason}" for reason in built.reasons),
            dropped_count=built.dropped_count + snapshot.dropped_log_fields,
            observed_count=snapshot.observed_logs,
            retained_count=built.retained_count,
            _encoded_size=built.encoded_size,
        )

    def _fit_evidence(
        self,
        association: _Association,
        spans: list[Mapping[str, Any]],
        reasons: tuple[str, ...],
        dropped: int,
        truncated: bool,
    ) -> tuple[Mapping[str, Any] | None, tuple[str, ...], int, int]:
        """Prune sampled spans until the complete envelope fits remaining Run bytes."""
        ordered = sorted(
            spans,
            key=lambda item: (
                item["start_time_unix_nano"],
                item["trace_id"],
                item["span_id"],
            ),
        )
        bounded_reasons = set(reasons) & _CAPTURE_REASONS
        run_key = (association.run_id, association.case_id)
        with self._lock:
            available = self.limits.max_total_bytes - self._run_bytes.get(run_key, 0)
        evidence = self._build_evidence(
            association, ordered, dropped, truncated, bounded_reasons
        )
        while ordered and json_size(evidence) > available:
            ordered.pop()
            if dropped >= _MAX_DROPPED_COUNT:
                bounded_reasons.add("trace_drop_count_saturated")
            else:
                dropped += 1
            truncated = True
            bounded_reasons.add("trace_byte_limit")
            evidence = self._build_evidence(
                association, ordered, dropped, truncated, bounded_reasons
            )
        encoded_size = json_size(evidence)
        if encoded_size <= available:
            return evidence, tuple(sorted(bounded_reasons)), dropped, encoded_size
        bounded_reasons.add("trace_budget_exhausted")
        if dropped >= _MAX_DROPPED_COUNT:
            bounded_reasons.add("trace_drop_count_saturated")
        dropped = min(dropped + 1, _MAX_DROPPED_COUNT)
        return None, tuple(sorted(bounded_reasons)), dropped, 0

    @staticmethod
    def _build_evidence(
        association: _Association,
        spans: list[Mapping[str, Any]],
        dropped: int,
        truncated: bool,
        reasons: set[str],
    ) -> dict[str, Any]:
        """Build the ordered Trace envelope for the current immutable snapshot."""
        return _evidence_payload(
            association.run_id,
            association.case_id,
            association.input_id,
            spans=spans,
            dropped_count=dropped,
            truncated=truncated,
            reasons=tuple(sorted(reasons)),
        )

    def _matching_association(
        self, run_id: str, case_id: str, input_id: str
    ) -> _Association:
        """Return the active association only when all supplied IDs match."""
        with self._lock:
            association = self._active
            expected = (run_id, case_id, input_id)
            actual = None
            if association is not None:
                actual = (association.run_id, association.case_id, association.input_id)
            if association is None or not association.active or actual != expected:
                raise RuntimeError(
                    "Trace Evidence association does not match this step"
                )
            return association

    def _flush(self, association: _Association) -> None:
        """Flush provider processors and record failure without breaking Submission."""
        if self._force_flush is not None:
            try:
                if not self._force_flush():
                    self.record_failure("trace_flush_failed", association=association)
            except Exception:
                self.record_failure("trace_flush_failed", association=association)

    def _snapshot(self, association: _Association) -> _TraceSnapshot:
        """Close the step window and copy its telemetry/accounting under lock.

        Registered spans still open at this boundary are observable omissions:
        each is charged once as ``trace_span_outside_window`` before its
        registration is removed. Spans wholly outside the window have no safe
        step association and are never reassigned to another Run.
        """
        with self._lock:
            association.active = False
            if self._active is association:
                self._active = None
            bucket = self._buckets[id(association)]
            still_open = sum(
                candidate is association for candidate in self._registrations.values()
            ) + len(self._prewindow_registrations)
            if still_open:
                bucket.observed_spans += still_open
                self._note_drop(
                    bucket,
                    still_open,
                    "trace_span_outside_window",
                    truncated=True,
                )
            self._drop_registrations_locked(association)
            self._prewindow_registrations.clear()
            sequence = bucket.next_sequence - 1
            spans = [item[1] for item in bucket.spans if item[0] <= sequence]
            reasons = tuple(sorted(bucket.reasons))
            dropped = bucket.dropped_count
            truncated = bucket.truncated
            observed_spans = bucket.observed_spans
            dropped_attribute_events = bucket.dropped_attribute_events
            ordered_logs = sorted(bucket.log_records, key=log_record_sort_key)
            log_records = [item[1] for item in ordered_logs]
            observed_logs = bucket.observed_logs
            dropped_logs = bucket.dropped_logs
            dropped_log_fields = bucket.dropped_log_fields
            log_reasons = tuple(sorted(bucket.log_reasons))
        spans, topology_dropped = _topology_closed(spans)
        if topology_dropped:
            reason_set = set(reasons)
            reason_set.add("trace_topology_partial")
            reasons = tuple(sorted(reason_set))
            dropped = min(dropped + topology_dropped, _MAX_DROPPED_COUNT)
            truncated = True
        return _TraceSnapshot(
            sequence=sequence,
            spans=spans,
            reasons=reasons,
            dropped=dropped,
            truncated=truncated,
            observed_spans=observed_spans,
            dropped_attribute_events=dropped_attribute_events,
            log_records=log_records,
            observed_logs=observed_logs,
            dropped_logs=dropped_logs,
            dropped_log_fields=dropped_log_fields,
            log_reasons=log_reasons,
        )

    def cancel_step(self) -> None:
        """Abort the active step so late telemetry cannot enter another input."""
        with self._lock:
            association = self._active
            if association is not None:
                self._discard_locked(association)

    def finish_run(self, run_id: str, case_id: str) -> None:
        """Seal Run capture and discard registrations that could leak across Runs."""
        with self._lock:
            association = self._active
            if association is not None and (
                association.run_id,
                association.case_id,
            ) == (run_id, case_id):
                self._discard_locked(association)
            self._run_bytes.pop((run_id, case_id), None)
            self._run_log_bytes.pop((run_id, case_id), None)
            self._prewindow_registrations.clear()

    def _commit(
        self,
        association: _Association,
        sequence: int,
        encoded_size: int,
        encoded_log_size: int,
    ) -> None:
        """Commit prepared byte usage and discard the active step transaction."""
        with self._lock:
            bucket = self._buckets.get(id(association))
            if bucket is None:
                return
            run_key = (association.run_id, association.case_id)
            self._run_bytes[run_key] = self._run_bytes.get(run_key, 0) + encoded_size
            self._run_log_bytes[run_key] = (
                self._run_log_bytes.get(run_key, 0) + encoded_log_size
            )
            retained = [item for item in bucket.spans if item[0] > sequence]
            if retained:
                bucket.spans = retained
                bucket.bytes_used = sum(item[2] for item in retained)
            else:
                self._buckets.pop(id(association), None)

    def _abort(self, association: _Association) -> None:
        """Abort prepared telemetry without advancing the committed Run budget."""
        with self._lock:
            if id(association) not in self._buckets:
                return
            if self._active is not None and self._active is not association:
                raise RuntimeError("Another Trace Evidence step is already active")
            association.active = True
            self._active = association

    def _discard_locked(self, association: _Association) -> None:
        """Remove one association and all late-telemetry registrations under lock."""
        association.active = False
        if self._active is association:
            self._active = None
        self._drop_registrations_locked(association)
        self._prewindow_registrations.clear()
        self._buckets.pop(id(association), None)

    def _drop_registrations_locked(self, association: _Association) -> None:
        """Delete span registrations owned by the completed or cancelled step."""
        stale = [
            key
            for key, candidate in self._registrations.items()
            if candidate is association
        ]
        for key in stale:
            self._registrations.pop(key, None)

    @staticmethod
    def _increment_dropped(bucket: _Bucket, count: int) -> None:
        """Saturate the dropped counter at its public contract maximum."""
        if count <= 0:
            return
        total = bucket.dropped_count + count
        bucket.dropped_count = min(total, _MAX_DROPPED_COUNT)
        if total > _MAX_DROPPED_COUNT:
            bucket.reasons.add("trace_drop_count_saturated")

    def _note_drop(
        self, bucket: _Bucket, count: int, reason: str, *, truncated: bool = False
    ) -> None:
        """Account for one dropped span and add its bounded reason code."""
        self._increment_dropped(bucket, count)
        bucket.reasons.add(
            reason if reason in _CAPTURE_REASONS else "trace_capture_failed"
        )
        bucket.truncated = bucket.truncated or truncated

    @staticmethod
    def _drop_log(bucket: _Bucket, reason: str) -> None:
        """Account for one dropped LogRecord and its bounded reason code."""
        bucket.dropped_logs = min(bucket.dropped_logs + 1, _MAX_DROPPED_COUNT)
        allowed = {
            "otel_log_byte_limit",
            "otel_log_capture_failed",
            "otel_log_export_failed",
            "otel_log_invalid",
            "otel_log_limit",
        }
        bucket.log_reasons.add(
            reason if reason in allowed else "otel_log_capture_failed"
        )


def _span_key(span: Any) -> tuple[int, int] | None:
    """Return a stable trace/span identifier pair or ``None`` for invalid spans."""
    context = getattr(span, "context", None)
    if context is None:
        get_context = getattr(span, "get_span_context", None)
        context = get_context() if callable(get_context) else None
    trace_id = getattr(context, "trace_id", None)
    span_id = getattr(context, "span_id", None)
    if (
        isinstance(trace_id, bool)
        or not isinstance(trace_id, int)
        or isinstance(span_id, bool)
        or not isinstance(span_id, int)
    ):
        return None
    return trace_id, span_id


def _span_priority(
    span: Mapping[str, Any], protected_parent_ids: set[str]
) -> tuple[int, int, str]:
    """Rank spans deterministically to preserve boundaries and topology under limits."""
    span_id = span["span_id"]
    if span["parent_span_id"] is None:
        category = 0
    elif span["status"] == "error":
        category = 1
    elif span_id in protected_parent_ids:
        category = 2
    elif _is_boundary_span(span):
        category = 3
    else:
        category = 4
    duration_rank = -span["duration_nano"] if category >= 3 else 0
    digest = sha256(f"{span['trace_id']}:{span_id}".encode("ascii")).hexdigest()
    return category, duration_rank, digest


def _is_boundary_span(span: Mapping[str, Any]) -> bool:
    """Return whether a span represents a root, leaf, error, or Agent boundary."""
    if span["kind"] in {"client", "server", "producer", "consumer"}:
        return True
    return span["attributes"].get("gen_ai.operation.name") == "execute_tool"


def _topology_closed(
    spans: list[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], int]:
    """Return whether retained parents are present whenever they were observed."""
    retained = list(spans)
    while True:
        identities = {(span["trace_id"], span["span_id"]) for span in retained}
        closed = [
            span
            for span in retained
            if span["parent_span_id"] is None
            or (span["trace_id"], span["parent_span_id"]) in identities
        ]
        if len(closed) == len(retained):
            return closed, len(spans) - len(closed)
        retained = closed


def _capture_summary(
    *,
    observed_spans: int,
    retained_spans: int,
    dropped_attribute_events: int,
    topology_complete: bool,
    observed_logs: int,
    retained_logs: int,
    dropped_log_fields: int,
) -> dict[str, Any]:
    """Compute final capture status, missing reasons, and dropped accounting."""
    return {
        "schema_version": "defuzex.trace_capture_summary.v1",
        "sampling_policy": "deterministic_topology_v1",
        "observed_spans": observed_spans,
        "retained_spans": retained_spans,
        "dropped_spans": max(0, observed_spans - retained_spans),
        "dropped_attributes_events": dropped_attribute_events,
        "topology_complete": topology_complete,
        "observed_log_records": observed_logs,
        "retained_log_records": retained_logs,
        "dropped_log_records": max(0, observed_logs - retained_logs),
        "dropped_log_fields": dropped_log_fields,
    }


__all__ = [
    "PreparedOtelLogs",
    "PreparedTraceEvidence",
    "TraceEvidenceCapture",
    "TraceEvidenceLimits",
]
