"""Coordinate per-step snapshots, logs, privacy checks, and local evidence."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from ...contracts import (
    CaptureComponent,
    CaptureStatus,
    FileEvidence,
    Submission,
)
from ...errors import ConfigurationError, EvidenceCaptureError, InputProtocolError
from ...repository.privacy import (
    SensitiveFinding,
    enforce_sensitive_policy,
    scan_sensitive_json,
    scan_sensitive_path,
    scan_sensitive_text,
)
from ..runtime import build_runtime_evidence, runtime_submission_id
from ..trace import (
    PreparedOtelLogs,
    PreparedTraceEvidence,
    TraceEvidenceCapture,
)
from .diff import DiffResult, compare_snapshots
from .logs import LogTracker, PreparedLogs
from .snapshot import Snapshot, Snapshotter


def _plain(value: Any) -> Any:
    """Return JSON-compatible Evidence data without retaining mutable aliases."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    if isinstance(value, Path):
        return value.as_posix()
    return value


def _failed_snapshot(root: Path) -> Snapshot:
    """Create an explicit failed Snapshot placeholder after filesystem capture errors."""
    return Snapshot(root=root, entries={}, errors=("snapshot_failed",), complete=False)


def _snapshot_component(*snapshots: Snapshot) -> CaptureComponent:
    """Aggregate Snapshot statuses and bounded reason codes into one component."""
    errors = tuple(
        dict.fromkeys(error for snapshot in snapshots for error in snapshot.errors)
    )
    if not errors and all(snapshot.complete for snapshot in snapshots):
        return CaptureComponent(status="complete")
    if any(snapshot.entries for snapshot in snapshots):
        return CaptureComponent(status="partial", reasons=errors)
    return CaptureComponent(status="failed", reasons=errors or ("snapshot_failed",))


@dataclass(frozen=True, slots=True)
class _PreparedFiles:
    """Stage file capture outputs before Submission validation completes.

    Attributes:
        snapshot: Aggregate baseline/final snapshot completeness.
        diff: File comparison completeness.
        evidence: Public file Evidence, or ``None`` when tracking is disabled.
        result: Full comparison result including local-only diffs.
    """

    snapshot: CaptureComponent
    diff: CaptureComponent
    evidence: FileEvidence | None
    result: DiffResult | None


@dataclass(slots=True)
class _CaptureSummary:
    """Accumulate per-source status while preparing one Submission.

    Attributes:
        prepared_traces: Trace/OTel transaction staged for the same step.
        status: Public per-component capture status.
        missing: Mutable stable degradation reasons collected by the coordinator.
        dropped_count: Total Evidence observations omitted so far.
    """

    prepared_traces: PreparedTraceEvidence | None
    status: CaptureStatus
    missing: list[str]
    dropped_count: int


@dataclass(slots=True)
class PreparedEvidence:
    """Stage every Evidence source for one atomic Submission commit.

    Attributes:
        capture_status: Public completeness state for file, log, privacy, and Trace.
        file_evidence: Bounded file Evidence prepared for the Submission.
        logs: Ordered explicit and OTel log artifacts.
        missing: Stable reasons for unavailable or partial Evidence.
        dropped_count: Total omitted observations across sources.
        extensions: Versioned typed Evidence and safe capture summaries.
        _collector: Owner whose active step is finalized on commit.
        _prepared_logs: Incremental log offsets staged with this transaction.
        _prepared_traces: Trace byte/association state staged with this transaction.
        _pending_path: Temporary local Evidence record, when persistence is enabled.
        _final_path: Final record path installed only on commit.
        _finished: Whether commit or abort already finalized the transaction.
    """

    capture_status: CaptureStatus
    file_evidence: FileEvidence | None
    logs: tuple[Mapping[str, Any], ...]
    missing: tuple[str, ...]
    dropped_count: int
    extensions: Mapping[str, Any]
    _collector: EvidenceCollector
    _prepared_logs: PreparedLogs
    _prepared_traces: PreparedTraceEvidence | None = None
    _pending_path: Path | None = None
    _final_path: Path | None = None
    _finished: bool = False

    def commit(self, submission: Submission) -> None:
        """Commit prepared capture state only after the enclosing submission succeeds."""
        del submission
        if self._finished:
            return
        self._prepared_logs.commit()
        if self._pending_path is not None and self._final_path is not None:
            try:
                self._pending_path.replace(self._final_path)
            except OSError:
                self._collector.runtime_warnings.append("local_evidence_commit_failed")
                with suppress(OSError):
                    self._pending_path.unlink(missing_ok=True)
        if self._prepared_traces is not None:
            self._prepared_traces.commit()
        self._collector._finish_step()
        self._finished = True

    def abort(self) -> None:
        """Discard prepared capture state after a failed enclosing submission."""
        if self._finished:
            return
        self._prepared_logs.abort()
        if self._prepared_traces is not None:
            self._prepared_traces.abort()
        if self._pending_path is not None:
            with suppress(OSError):
                self._pending_path.unlink(missing_ok=True)
        self._finished = True


class EvidenceCollector:
    """Coordinate bounded step Evidence as one commit-or-abort transaction.

    ``Run.get_input`` starts a step, ``Run.submit`` calls :meth:`prepare`, and
    :class:`PreparedEvidence` commits only after immutable history append. This
    coupling prevents log offsets, Trace budgets, and local records from moving
    forward when Submission validation fails.

    Security/Privacy:
        Reads are restricted to the configured root and explicit log paths.
        Uploadable output passes size limits and sensitive scanning; private
        rubric/model credentials are never inputs to this collector.
    """

    def __init__(
        self,
        *,
        root: Path,
        root_alias: Path | None = None,
        scope: str,
        excluded_roots: tuple[Path, ...],
        track_files: bool,
        upload_diff: bool,
        save_local: bool,
        allow_sensitive: bool,
        block_sensitive: bool,
        persistent_path: Path | None,
        snapshotter: Snapshotter | None = None,
        log_tracker: LogTracker | None = None,
        run_id: str | None = None,
        case_id: str | None = None,
        trace_evidence: TraceEvidenceCapture | None = None,
    ) -> None:
        """Configure all Evidence sources for one Run.

        Args:
            root: Canonical filesystem root allowed for snapshot and log reads.
            root_alias: Optional caller-authorized absolute spelling retained
                before canonicalization. It must resolve exactly to ``root`` and
                is used only for platform aliases such as macOS ``/var``.
            scope: Public ``container`` or explicitly allowed ``local`` label.
            excluded_roots: Canonical subtrees never scanned, including KUMA
                runtime/persistence directories.
            track_files: Capture before/after snapshots when true.
            upload_diff: Include bounded safe text diffs in public Evidence.
            save_local: Stage atomic local Submission records when true.
            allow_sensitive: Opt-in used by supported ordinary Evidence policy.
            block_sensitive: Force rejection when official/trace boundaries make
                sensitive content unsafe regardless of local preference.
            persistent_path: Run-owned local output directory, or ``None``.
            snapshotter: Optional preconfigured filesystem boundary for tests.
            log_tracker: Optional incremental log tracker for tests/embedding.
            run_id: Owning Run identifier required for Trace/runtime Evidence.
            case_id: Owning Case identifier required for Trace correlation.
            trace_evidence: Optional in-process OTel capture.

        Preconditions:
            ``root`` is the repository/runtime scope authorized by ``create_run``;
            excluded roots and persistent path belong to that Run.

        Postconditions:
            Collector is idle with no active input or committed offsets changed.

        Side Effects:
            Resolves paths and allocates capture helpers; no snapshot/network/write
            occurs until lifecycle methods are called.
        """
        root_failed = False
        try:
            self.root = root.resolve()
            log_root = root if root_alias is None else root_alias
            if log_root.resolve() != self.root:
                raise ConfigurationError(
                    "Evidence root alias must match the repository root"
                )
        except ConfigurationError:
            raise
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
            root_failed = True
        if root_failed:
            raise EvidenceCaptureError("Evidence storage is unavailable") from None
        self.scope = scope
        self.track_files = track_files
        self.upload_diff = upload_diff
        self.save_local = save_local
        self.allow_sensitive = allow_sensitive
        self.block_sensitive = block_sensitive
        self.persistent_path = persistent_path
        helper_failed = False
        try:
            self.snapshotter = snapshotter or Snapshotter(
                self.root,
                excluded_roots=excluded_roots,
            )
            self.log_tracker = log_tracker or LogTracker(root=log_root)
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
            helper_failed = True
        if helper_failed:
            raise EvidenceCaptureError("Evidence storage is unavailable") from None
        if self.log_tracker.root != self.root:
            raise ConfigurationError("Evidence log root must match the repository root")
        self.run_id = run_id
        self.case_id = case_id
        self.trace_evidence = trace_evidence
        self._input_id: str | None = None
        self._baseline: Snapshot | None = None
        self._step_index = 0
        self.runtime_warnings: list[str] = []

    def begin_step(self, input_id: str) -> None:
        """Start baseline and Trace association for one delivered input.

        Args:
            input_id: Non-empty identifier of the current Run input.

        Raises:
            InputProtocolError: Another input already owns the active Evidence
                transaction.

        Preconditions:
            No other input is active; ``Run.get_input`` owns this call.

        Postconditions:
            The input is active. File baseline is captured or marked failed;
            Trace begin failure becomes a safe runtime warning and does not block.

        Side Effects:
            Reads a bounded repository snapshot and opens in-memory Trace capture.
        """
        if self._input_id == input_id:
            return
        if self._input_id is not None:
            raise InputProtocolError(
                "Evidence baseline already belongs to another Input"
            )
        self._input_id = input_id
        if self.trace_evidence is not None:
            try:
                self.trace_evidence.begin_step(
                    self.run_id or "", self.case_id or "", input_id
                )
            except Exception:
                self.runtime_warnings.append("trace_begin_failed")
        if not self.track_files:
            self._baseline = None
            return
        try:
            self._baseline = self.snapshotter.capture()
        except Exception:
            self._baseline = _failed_snapshot(self.root)

    def prepare(
        self,
        *,
        input_id: str,
        output: Any,
        status: str,
        error: str | None,
        logs: Sequence[str] | None,
    ) -> PreparedEvidence:
        """Stage all Evidence owned by one Input for transactional Submission.

        The coordinator combines ``Snapshotter`` file deltas, ``LogTracker``
        segments, in-process ``TraceEvidenceCapture`` data, privacy enforcement,
        canonical Runtime Evidence extensions, and an optional pending local
        record. Returned ``PreparedEvidence`` owns every staged resource: only
        ``commit(submission)`` advances offsets/budgets and atomically publishes
        the local record, while ``abort()`` rolls back those staged side effects.

        Args:
            input_id: Exact active input identifier; mismatches fail closed.
            output: Already validated JSON-compatible Agent output.
            status: Stable Submission outcome.
            error: Optional caller-safe error summary.
            logs: Validated ordered log paths selected by the caller, or
                ``None``. Relative values are resolved by ``LogTracker`` from
                this collector's canonical ``root``.

        Returns:
            :class:`PreparedEvidence` owning all staged resources. The caller must
            call exactly one of ``commit`` or ``abort``.

        Raises:
            InputProtocolError: If ``input_id`` is not the active step.
            SensitiveDataError: If prepared uploadable content violates policy.
            EvidenceCaptureError: If required canonical Evidence cannot be built.

        Preconditions:
            ``begin_step(input_id)`` succeeded and no prior preparation was
            committed for this input.

        Postconditions:
            Success has not advanced log offsets, Trace byte budgets, step index,
            or final local files. Those change only through ``commit``. Failure
            aborts staged log/Trace/local resources before propagating.

        Side Effects:
            Reads bounded final file state and explicit logs, flushes in-process
            OTel providers, and may write a temporary local JSON record.

        Security/Privacy:
            Output, error, diffs, logs, and trace projections are scanned/bounded;
            raw private rubric/model/provider data is not an input.
        """
        self._require_active_input(input_id)
        prepared_logs, prepared_files = self._prepare_sources(logs=logs)
        prepared_traces = self._prepare_traces(input_id)
        self._merge_otel_logs(
            prepared_logs, prepared_traces.otel_logs if prepared_traces else None
        )
        findings = self._scan_findings(
            output=output,
            error=error,
            diff_result=prepared_files.result,
            logs=prepared_logs.segments,
        )
        self._enforce_sensitive(findings, prepared_logs, prepared_traces)
        summary = self._capture_summary(
            prepared_logs=prepared_logs,
            prepared_files=prepared_files,
            findings=findings,
            prepared_traces=prepared_traces,
        )
        extensions = self._evidence_extensions(
            input_id=input_id,
            status=status,
            output=output,
            error=error,
            prepared_logs=prepared_logs,
            prepared_files=prepared_files,
            summary=summary,
            sensitive_detected=bool(findings),
        )
        pending_path, final_path = self._prepare_submission_record(
            input_id=input_id,
            status=status,
            output=output,
            error=error,
            prepared_logs=prepared_logs,
            prepared_files=prepared_files,
            summary=summary,
            extensions=extensions,
        )
        return PreparedEvidence(
            capture_status=summary.status,
            file_evidence=prepared_files.evidence,
            logs=prepared_logs.segments,
            missing=tuple(dict.fromkeys(summary.missing)),
            dropped_count=summary.dropped_count,
            extensions=extensions,
            _collector=self,
            _prepared_logs=prepared_logs,
            _prepared_traces=summary.prepared_traces,
            _pending_path=pending_path,
            _final_path=final_path,
        )

    def _require_active_input(self, input_id: str) -> None:
        """Require exact association with the currently active Run input."""
        if self._input_id != input_id:
            raise InputProtocolError(
                "Evidence capture does not match the current Input"
            )

    def _prepare_sources(
        self,
        *,
        logs: Sequence[str] | None,
    ) -> tuple[PreparedLogs, _PreparedFiles]:
        """Prepare file, explicit log, and Trace sources without committing them."""
        prepared_logs = self.log_tracker.prepare(logs)
        prepared_files = self._prepare_files()
        return prepared_logs, prepared_files

    def _enforce_sensitive(
        self,
        findings: list[SensitiveFinding],
        prepared_logs: PreparedLogs,
        prepared_traces: PreparedTraceEvidence | None,
    ) -> None:
        """Reject sensitive Evidence for upload while honoring explicit local policy."""
        if not self.block_sensitive:
            return
        try:
            enforce_sensitive_policy(findings, allow_sensitive=self.allow_sensitive)
        except BaseException:
            prepared_logs.abort()
            if prepared_traces is not None:
                prepared_traces.abort()
            raise

    def _capture_summary(
        self,
        *,
        prepared_logs: PreparedLogs,
        prepared_files: _PreparedFiles,
        findings: list[SensitiveFinding],
        prepared_traces: PreparedTraceEvidence | None,
    ) -> _CaptureSummary:
        """Combine component status, missing reasons, and dropped counts."""
        missing = list(prepared_logs.missing)
        dropped_count = prepared_logs.dropped_count
        if prepared_files.evidence is not None:
            missing.extend(prepared_files.evidence.errors)
            dropped_count += len(prepared_files.evidence.errors)
        trace_component, trace_missing, trace_dropped = self._trace_summary(
            prepared_traces
        )
        missing.extend(trace_missing)
        return _CaptureSummary(
            prepared_traces=prepared_traces,
            status=CaptureStatus(
                file_snapshot=prepared_files.snapshot,
                file_diff=prepared_files.diff,
                logs=prepared_logs.component,
                traces=trace_component,
                sensitive_scan=self._sensitive_component(findings),
            ),
            missing=missing,
            dropped_count=dropped_count + trace_dropped,
        )

    def _evidence_extensions(
        self,
        *,
        input_id: str,
        status: str,
        output: Any,
        error: str | None,
        prepared_logs: PreparedLogs,
        prepared_files: _PreparedFiles,
        summary: _CaptureSummary,
        sensitive_detected: bool,
    ) -> dict[str, Any]:
        """Build backward-compatible Trace and canonical Runtime Evidence extensions."""
        extensions: dict[str, Any] = {
            "allow_sensitive": self.allow_sensitive,
            "sensitive_detected": sensitive_detected,
        }
        trace_evidence = None
        if (
            summary.prepared_traces is not None
            and summary.prepared_traces.evidence is not None
        ):
            trace_evidence = summary.prepared_traces.evidence
            extensions["trace_evidence"] = trace_evidence
            if summary.prepared_traces.capture_summary is not None:
                extensions["trace_capture_summary"] = (
                    summary.prepared_traces.capture_summary
                )
        if self.run_id and self.case_id:
            built = build_runtime_evidence(
                run_id=self.run_id,
                input_id=input_id,
                step_id=input_id,
                submission_id=runtime_submission_id(self.run_id, input_id),
                root=self.root,
                status=status,
                output=output,
                error=error,
                file_evidence=prepared_files.evidence,
                logs=prepared_logs.segments,
                trace_evidence=trace_evidence,
            )
            extensions["runtime_evidence"] = built.evidence
            summary.missing.extend(built.missing)
            summary.dropped_count += built.dropped_count
        return extensions

    def _prepare_submission_record(
        self,
        *,
        input_id: str,
        status: str,
        output: Any,
        error: str | None,
        prepared_logs: PreparedLogs,
        prepared_files: _PreparedFiles,
        summary: _CaptureSummary,
        extensions: Mapping[str, Any],
    ) -> tuple[Path | None, Path | None]:
        """Build the optional local history record without writing it yet."""
        record = {
            "input_id": input_id,
            "status": status,
            "output": _plain(output),
            "error": error,
            "capture_status": _plain(summary.status),
            "file_evidence": _plain(prepared_files.evidence),
            "local_diffs": (
                {}
                if prepared_files.result is None
                else dict(prepared_files.result.local_diffs)
            ),
            "logs": _plain(prepared_logs.segments),
            "missing": summary.missing,
            "dropped_count": summary.dropped_count,
            "extensions": extensions,
        }
        pending_path, final_path, local_failed = self._prepare_local_record(record)
        if local_failed:
            summary.missing.append("local_evidence_prepare_failed")
            summary.dropped_count += 1
        return pending_path, final_path

    def trace_output(self, input_id: str) -> Any | None:
        """Return auto-submittable output from the active OTel step, if present."""

        if self._input_id != input_id or self.trace_evidence is None:
            return None
        try:
            return self.trace_evidence.output_for_step(
                self.run_id or "", self.case_id or "", input_id
            )
        except Exception:
            if "trace_output_unavailable" not in self.runtime_warnings:
                self.runtime_warnings.append("trace_output_unavailable")
            return None

    def _trace_summary(
        self, prepared: PreparedTraceEvidence | None
    ) -> tuple[CaptureComponent, tuple[str, ...], int]:
        """Project Trace preparation accounting into Submission capture metadata."""
        if prepared is None:
            return CaptureComponent(), (), 0
        for reason in prepared.component.reasons:
            warning = f"trace_evidence:{reason}"
            if warning not in self.runtime_warnings:
                self.runtime_warnings.append(warning)
        return prepared.component, prepared.missing, prepared.dropped_count

    def _merge_otel_logs(
        self, prepared_logs: PreparedLogs, otel_logs: PreparedOtelLogs | None
    ) -> None:
        """Merge native OTel Logs after explicit logs with deterministic segment numbers."""
        if otel_logs is None or otel_logs.component.status == "skipped":
            return
        prepared_logs.segments += otel_logs.segments
        prepared_logs.missing += otel_logs.missing
        prepared_logs.dropped_count += otel_logs.dropped_count
        for reason in otel_logs.component.reasons:
            warning = f"otel_logs:{reason}"
            if warning not in self.runtime_warnings:
                self.runtime_warnings.append(warning)
        if prepared_logs.missing:
            status = "partial" if prepared_logs.segments else "failed"
        else:
            status = "complete"
        prepared_logs.component = CaptureComponent(
            status=status,
            reasons=tuple(dict.fromkeys(prepared_logs.missing)),
        )

    def _prepare_traces(self, input_id: str) -> PreparedTraceEvidence | None:
        """Prepare associated Trace Evidence and isolate capture failures."""
        if self.trace_evidence is None:
            return None
        try:
            return self.trace_evidence.prepare_step(
                self.run_id or "", self.case_id or "", input_id
            )
        except Exception:
            self.runtime_warnings.append("trace_capture_failed")
            try:
                self.trace_evidence.cancel_step()
            except Exception:
                self.runtime_warnings.append("trace_cancel_failed")
            return PreparedTraceEvidence(
                evidence=None,
                component=CaptureComponent(
                    status="failed", reasons=("trace_capture_failed",)
                ),
                missing=("trace_evidence:trace_capture_failed",),
                dropped_count=1,
                capture_summary=None,
                otel_logs=PreparedOtelLogs(),
                _capture=self.trace_evidence,
                _association=None,
                _sequence=-1,
            )

    def _prepare_files(self) -> _PreparedFiles:
        """Capture the after-Snapshot and diff it from the active step baseline."""
        if not self.track_files:
            skipped = CaptureComponent(status="skipped")
            return _PreparedFiles(skipped, skipped, None, None)

        baseline = self._baseline or _failed_snapshot(self.root)
        try:
            after = self.snapshotter.capture()
        except Exception:
            after = _failed_snapshot(self.root)
        try:
            result = compare_snapshots(
                baseline,
                after,
                scope=self.scope,
                upload_diff=self.upload_diff,
            )
        except Exception:
            result = DiffResult(
                evidence=FileEvidence(
                    complete=False,
                    scope=self.scope,
                    errors=("diff_failed",),
                ),
                local_diffs={},
            )
        diff = CaptureComponent(
            status="complete" if result.evidence.complete else "partial",
            reasons=result.evidence.errors,
        )
        return _PreparedFiles(
            snapshot=_snapshot_component(baseline, after),
            diff=diff,
            evidence=result.evidence,
            result=result,
        )

    def _scan_findings(
        self,
        *,
        output: Any,
        error: str | None,
        diff_result: DiffResult | None,
        logs: tuple[Mapping[str, Any], ...],
    ) -> list[SensitiveFinding]:
        """Scan Agent output, error, logs, file paths, and diff text for secrets."""
        findings = list(scan_sensitive_json(output, location="output"))
        if error is not None:
            findings.extend(scan_sensitive_text(error, location="error"))
        if diff_result is not None:
            for change in diff_result.evidence.changes:
                findings.extend(scan_sensitive_path(change.path, location="file_path"))
                if change.old_path is not None:
                    findings.extend(
                        scan_sensitive_path(change.old_path, location="file_path")
                    )
            if self.upload_diff:
                for text in diff_result.local_diffs.values():
                    findings.extend(scan_sensitive_text(text, location="file_diff"))
        for segment in logs:
            findings.extend(
                scan_sensitive_path(str(segment["path"]), location="log_path")
            )
            findings.extend(
                scan_sensitive_text(str(segment["content"]), location="log")
            )
        return findings

    def _sensitive_component(
        self, findings: list[SensitiveFinding]
    ) -> CaptureComponent:
        """Mark Evidence partial when sensitive findings are locally allowed."""
        if not findings:
            return CaptureComponent(status="complete")
        reason = (
            "allow_sensitive_override"
            if self.allow_sensitive
            else "sensitive_content_local_only"
        )
        return CaptureComponent(
            status="complete",
            reasons=(f"{reason}:{len(findings)}",),
        )

    def _prepare_local_record(
        self, record: Mapping[str, Any]
    ) -> tuple[Path | None, Path | None, bool]:
        """Stage a redacted JSON history record for atomic Submission commit.

        Args:
            record: Already privacy-filtered Submission record to serialize.

        Returns:
            Pending/final paths and ``False`` on success; ``(None, None, True)``
            when bounded local persistence degrades without blocking the Run.

        Postconditions:
            Success transfers the pending file to :class:`PreparedEvidence`.
            Failure closes any descriptor not yet owned by ``fdopen`` and removes
            the partial file, so no local commit is implied.

        Side Effects:
            Creates the Run-owned submissions directory and one temporary file;
            it performs no network operation.
        """
        if not self.save_local or self.persistent_path is None:
            return None, None, False
        submissions = self.persistent_path / "submissions"
        descriptor: int | None = None
        pending_path: Path | None = None
        try:
            submissions.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".step-{self._step_index:04d}-",
                suffix=".pending",
                dir=submissions,
                text=True,
            )
            pending_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            final_path = submissions / f"step-{self._step_index:04d}.json"
            return pending_path, final_path, False
        except (OSError, TypeError, ValueError):
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            if pending_path is not None:
                with suppress(OSError):
                    pending_path.unlink(missing_ok=True)
            return None, None, True

    def cancel_step(self) -> None:
        """Abort the active step so late telemetry cannot enter another input."""
        if self.trace_evidence is not None:
            try:
                self.trace_evidence.cancel_step()
            except Exception:
                self.runtime_warnings.append("trace_cancel_failed")
            self.finish_run()
        self._input_id = None
        self._baseline = None

    def finish_run(self) -> None:
        """Seal Run capture and discard registrations that could leak across Runs."""
        if self.trace_evidence is None:
            return
        try:
            self.trace_evidence.finish_run(self.run_id or "", self.case_id or "")
        except Exception:
            self.runtime_warnings.append("trace_finish_failed")

    def _finish_step(self) -> None:
        """Clear active step state after commit, abort, or cancellation."""
        self._input_id = None
        self._baseline = None
        self._step_index += 1


__all__ = ["EvidenceCollector", "PreparedEvidence"]
