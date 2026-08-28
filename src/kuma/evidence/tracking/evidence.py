"""Coordinate per-step snapshots, logs, privacy checks, and local evidence."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from ...contracts import (
    CaptureComponent,
    CaptureStatus,
    FileEvidence,
    Submission,
)
from ...errors import InputProtocolError
from ...repository.privacy import (
    SensitiveFinding,
    enforce_sensitive_policy,
    scan_sensitive_json,
    scan_sensitive_path,
    scan_sensitive_text,
)
from ..runtime import build_runtime_evidence, runtime_submission_id
from ..trace import PreparedTraceEvidence, TraceEvidenceCapture
from .diff import DiffResult, compare_snapshots
from .logs import LogTracker, PreparedLogs
from .snapshot import Snapshot, Snapshotter


def _plain(value: Any) -> Any:
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
    return Snapshot(root=root, entries={}, errors=("snapshot_failed",), complete=False)


def _metadata_capture_complete(snapshot: Snapshot) -> bool:
    # Text retention only affects local diff availability; file metadata and
    # hashes remain complete and keep their separate capture status.
    return snapshot.complete or (
        bool(snapshot.errors)
        and all(error == "text_size_limit" for error in snapshot.errors)
    )


def _snapshot_component(*snapshots: Snapshot) -> CaptureComponent:
    errors = tuple(
        dict.fromkeys(error for snapshot in snapshots for error in snapshot.errors)
    )
    if all(_metadata_capture_complete(snapshot) for snapshot in snapshots):
        return CaptureComponent(status="complete")
    if any(snapshot.entries for snapshot in snapshots):
        return CaptureComponent(status="partial", reasons=errors)
    return CaptureComponent(status="failed", reasons=errors or ("snapshot_failed",))


@dataclass(frozen=True, slots=True)
class _PreparedFiles:
    snapshot: CaptureComponent
    diff: CaptureComponent
    evidence: FileEvidence | None
    result: DiffResult | None


@dataclass(slots=True)
class PreparedEvidence:
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
        del submission
        if self._finished:
            return
        self._prepared_logs.commit()
        if self._pending_path is not None and self._final_path is not None:
            try:
                self._pending_path.replace(self._final_path)
            except OSError:
                self._collector.runtime_warnings.append("local_evidence_commit_failed")
                self._pending_path.unlink(missing_ok=True)
        if self._prepared_traces is not None:
            self._prepared_traces.commit()
        self._collector._finish_step()
        self._finished = True

    def abort(self) -> None:
        if self._finished:
            return
        self._prepared_logs.abort()
        if self._prepared_traces is not None:
            self._prepared_traces.abort()
        if self._pending_path is not None:
            self._pending_path.unlink(missing_ok=True)
        self._finished = True


class EvidenceCollector:
    def __init__(
        self,
        *,
        root: Path,
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
        self.root = root.resolve()
        self.scope = scope
        self.track_files = track_files
        self.upload_diff = upload_diff
        self.save_local = save_local
        self.allow_sensitive = allow_sensitive
        self.block_sensitive = block_sensitive
        self.persistent_path = persistent_path
        self.snapshotter = snapshotter or Snapshotter(
            self.root,
            excluded_roots=excluded_roots,
        )
        self.log_tracker = log_tracker or LogTracker()
        self.run_id = run_id
        self.case_id = case_id
        self.trace_evidence = trace_evidence
        self._input_id: str | None = None
        self._baseline: Snapshot | None = None
        self._step_index = 0
        self.runtime_warnings: list[str] = []

    def begin_step(self, input_id: str) -> None:
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
        logs: list[str | os.PathLike[str]] | None,
    ) -> PreparedEvidence:
        if self._input_id != input_id:
            raise InputProtocolError(
                "Evidence capture does not match the current Input"
            )
        prepared_logs = self.log_tracker.prepare(logs)
        prepared_files = self._prepare_files()
        findings = self._scan_findings(
            output=output,
            error=error,
            diff_result=prepared_files.result,
            logs=prepared_logs.segments,
        )
        if self.block_sensitive:
            try:
                enforce_sensitive_policy(findings, allow_sensitive=self.allow_sensitive)
            except BaseException:
                prepared_logs.abort()
                raise

        missing = list(prepared_logs.missing)
        dropped_count = prepared_logs.dropped_count
        if prepared_files.evidence is not None:
            missing.extend(prepared_files.evidence.errors)
            dropped_count += len(prepared_files.evidence.errors)
        prepared_traces, trace_component, trace_missing, trace_dropped = (
            self._trace_summary(input_id)
        )
        missing.extend(trace_missing)
        dropped_count += trace_dropped
        capture_status = CaptureStatus(
            file_snapshot=prepared_files.snapshot,
            file_diff=prepared_files.diff,
            logs=prepared_logs.component,
            traces=trace_component,
            sensitive_scan=self._sensitive_component(findings),
        )
        extensions = {
            "allow_sensitive": self.allow_sensitive,
            "sensitive_detected": bool(findings),
        }
        trace_evidence = None
        if prepared_traces is not None and prepared_traces.evidence is not None:
            trace_evidence = prepared_traces.evidence
            extensions["trace_evidence"] = trace_evidence
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
            missing.extend(built.missing)
            dropped_count += built.dropped_count
        record = {
            "input_id": input_id,
            "status": status,
            "output": _plain(output),
            "error": error,
            "capture_status": _plain(capture_status),
            "file_evidence": _plain(prepared_files.evidence),
            "local_diffs": (
                {}
                if prepared_files.result is None
                else dict(prepared_files.result.local_diffs)
            ),
            "logs": _plain(prepared_logs.segments),
            "missing": missing,
            "dropped_count": dropped_count,
            "extensions": extensions,
        }
        pending_path, final_path, local_failed = self._prepare_local_record(record)
        if local_failed:
            missing.append("local_evidence_prepare_failed")
            dropped_count += 1

        return PreparedEvidence(
            capture_status=capture_status,
            file_evidence=prepared_files.evidence,
            logs=prepared_logs.segments,
            missing=tuple(dict.fromkeys(missing)),
            dropped_count=dropped_count,
            extensions=extensions,
            _collector=self,
            _prepared_logs=prepared_logs,
            _prepared_traces=prepared_traces,
            _pending_path=pending_path,
            _final_path=final_path,
        )

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
        self, input_id: str
    ) -> tuple[
        PreparedTraceEvidence | None,
        CaptureComponent,
        tuple[str, ...],
        int,
    ]:
        prepared = self._prepare_traces(input_id)
        if prepared is None:
            return None, CaptureComponent(), (), 0
        for reason in prepared.component.reasons:
            warning = f"trace_evidence:{reason}"
            if warning not in self.runtime_warnings:
                self.runtime_warnings.append(warning)
        return prepared, prepared.component, prepared.missing, prepared.dropped_count

    def _prepare_traces(self, input_id: str) -> PreparedTraceEvidence | None:
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
                _capture=self.trace_evidence,
                _association=None,
                _sequence=-1,
            )

    def _prepare_files(self) -> _PreparedFiles:
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
        if not self.save_local or self.persistent_path is None:
            return None, None, False
        submissions = self.persistent_path / "submissions"
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
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            final_path = submissions / f"step-{self._step_index:04d}.json"
            return pending_path, final_path, False
        except (OSError, TypeError, ValueError):
            if pending_path is not None:
                pending_path.unlink(missing_ok=True)
            return None, None, True

    def cancel_step(self) -> None:
        if self.trace_evidence is not None:
            try:
                self.trace_evidence.cancel_step()
            except Exception:
                self.runtime_warnings.append("trace_cancel_failed")
            self.finish_run()
        self._input_id = None
        self._baseline = None

    def finish_run(self) -> None:
        if self.trace_evidence is None:
            return
        try:
            self.trace_evidence.finish_run(self.run_id or "", self.case_id or "")
        except Exception:
            self.runtime_warnings.append("trace_finish_failed")

    def _finish_step(self) -> None:
        self._input_id = None
        self._baseline = None
        self._step_index += 1


__all__ = ["EvidenceCollector", "PreparedEvidence"]
