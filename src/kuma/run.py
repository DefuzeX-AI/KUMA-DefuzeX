"""The single strict-handshake Run state machine shared by all Providers."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from .contracts import (
    CaptureStatus,
    Case,
    HistoryItem,
    KumaInput,
    Submission,
    TestReport,
)
from .errors import (
    ConfigurationError,
    EvidenceCaptureError,
    InputProtocolError,
    KumaError,
    ProviderError,
    ValidationError,
)
from .evidence.tracking.evidence import EvidenceCollector, PreparedEvidence
from .providers.base import JudgeContext, JudgeProvider
from .providers.normalization import normalize_report
from .runtime import RuntimeSession

RunState = Literal[
    "ready",
    "input_delivered",
    "submitting",
    "completed",
    "judging",
    "report_ready",
    "cancelled",
    "failed",
]
_OUTPUT_UNSET = object()


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(child) for child in value]
    if isinstance(value, list):
        return [_plain_json(child) for child in value]
    return value


def _validate_json(value: Any, description: str) -> Any:
    plain = _plain_json(value)
    try:
        json.dumps(plain, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{description} must be JSON serializable", code="output_invalid"
        ) from exc
    return plain


class Run:
    """Synchronous delivery, submission, and Judge lifecycle for one Case."""

    def __init__(
        self,
        *,
        run_id: str,
        case: Case,
        runtime: RuntimeSession,
        judge_provider: JudgeProvider | None,
        judge_enabled: bool,
        on_failure: str,
        strategy: str,
        evidence: EvidenceCollector | None = None,
    ) -> None:
        self.run_id = run_id
        self.case_id = case.case_id or ""
        self.strategy = strategy
        self.max_inputs = len(case.inputs)
        self._case = case
        self._runtime = runtime
        self._judge_provider = judge_provider
        self._judge_enabled = judge_enabled
        self._on_failure = on_failure
        self._state: RunState = "ready"
        self._index = 0
        self._current: KumaInput | None = None
        self._history: list[HistoryItem] = []
        self._report: TestReport | None = None
        self._stopped_early = False
        self._evidence = evidence
        self._mutex = threading.RLock()

    @property
    def state(self) -> RunState:
        """Return the current synchronous state-machine state."""

        with self._mutex:
            return self._state

    @property
    def history(self) -> tuple[HistoryItem, ...]:
        """Return a stable snapshot of committed Input/Submission pairs."""

        with self._mutex:
            return tuple(self._history)

    @property
    def report(self) -> TestReport | None:
        """Return the normalized Judgment after the state reaches report_ready."""

        with self._mutex:
            return self._report

    @property
    def runtime_warnings(self) -> tuple[str, ...]:
        """Return non-fatal Evidence degradation observed during this Run."""

        with self._mutex:
            if self._evidence is None:
                return ()
            return tuple(self._evidence.runtime_warnings)

    def get_input(self, *, full: bool = False) -> Any:
        """Deliver the current Input without advancing until it is submitted.

        By default only the JSON-compatible payload is returned. ``full=True``
        returns the immutable :class:`KumaInput` with public identifiers and
        constraints. Repeated calls before ``submit`` return the same Input.
        """

        with self._mutex:
            if self._state == "input_delivered":
                current = self._current
            elif self._state == "ready":
                if self._index >= len(self._case.inputs):
                    return None
                current = self._case.inputs[self._index]
                if self._evidence is not None:
                    self._evidence.begin_step(current.input_id)
                self._current = current
                self._state = "input_delivered"
            elif self._state in {"completed", "report_ready"}:
                return None
            else:
                raise InputProtocolError(
                    f"get_input() is not allowed while Run is {self._state}"
                )
            if current is None:
                raise InputProtocolError("Run has no current Input")
            return current if full else _plain_json(current.payload)

    def submit(
        self,
        output: Any = _OUTPUT_UNSET,
        *,
        status: str = "completed",
        error: str | None = None,
        logs: list[str | Path] | None = None,
        wait: bool = True,
    ) -> TestReport | None:
        """Validate and commit one result, then advance or judge synchronously.

        When ``output`` is omitted, a completed submission uses the current
        OpenTelemetry ``invoke_agent``/``invoke_workflow`` output. Agents without
        that semantic-convention output pass ``output`` explicitly. Evidence
        offsets, local final files, and Trace byte budgets advance only after the
        Submission is successfully appended to history. On the final Input this
        returns a report when Judge is enabled, otherwise ``None``.
        """

        with self._mutex:
            current = self._submission_input(logs)
            plain_output = self._validated_output(
                output,
                current=current,
                status=status,
                error=error,
            )
            prepared = self._prepare_evidence(
                current=current,
                output=plain_output,
                status=status,
                error=error,
                logs=logs,
            )
            submission = self._submission(
                current=current,
                output=plain_output,
                status=status,
                error=error,
                prepared=prepared,
            )
            self._record_submission(current, submission, prepared)
            return self._advance_after_submission(status=status, wait=wait)

    def _submission_input(self, logs: list[str | Path] | None) -> KumaInput:
        if self._state != "input_delivered" or self._current is None:
            raise InputProtocolError(
                f"submit() requires a delivered Input; Run is {self._state}"
            )
        if logs and self._evidence is None:
            raise EvidenceCaptureError(
                "Log capture is not enabled for this Run",
                code="evidence_required",
            )
        return self._current

    def _validated_output(
        self,
        output: Any,
        *,
        current: KumaInput,
        status: str,
        error: str | None,
    ) -> Any:
        if error is not None and not isinstance(error, str):
            raise ValidationError("error must be text or None", code="output_invalid")
        if status not in {"completed", "failed", "timeout", "aborted"}:
            raise ValidationError("Invalid submission status", code="output_invalid")
        if output is _OUTPUT_UNSET:
            output = None
            if status == "completed" and self._evidence is not None:
                output = self._evidence.trace_output(current.input_id)
        plain_output = _validate_json(output, "output") if output is not None else None
        if status == "completed" and plain_output is None:
            raise ValidationError(
                "No OpenTelemetry Agent/Workflow output was available; pass the "
                "Agent result explicitly with submit(output)",
                code="output_invalid",
            )
        return plain_output

    def _prepare_evidence(
        self,
        *,
        current: KumaInput,
        output: Any,
        status: str,
        error: str | None,
        logs: list[str | Path] | None,
    ) -> PreparedEvidence | None:
        if self._evidence is None:
            return None
        return self._evidence.prepare(
            input_id=current.input_id,
            output=output,
            status=status,
            error=error,
            logs=logs,
        )

    def _submission(
        self,
        *,
        current: KumaInput,
        output: Any,
        status: str,
        error: str | None,
        prepared: PreparedEvidence | None,
    ) -> Submission:
        try:
            return Submission(
                run_id=self.run_id,
                case_id=self.case_id,
                input_id=current.input_id,
                status=status,
                output=output,
                error=error,
                capture_status=(
                    prepared.capture_status if prepared is not None else CaptureStatus()
                ),
                logs=() if prepared is None else prepared.logs,
                file_evidence=None if prepared is None else prepared.file_evidence,
                missing=() if prepared is None else prepared.missing,
                dropped_count=0 if prepared is None else prepared.dropped_count,
                extensions={} if prepared is None else prepared.extensions,
            )
        except ValidationError:
            if prepared is not None:
                prepared.abort()
            raise

    def _record_submission(
        self,
        current: KumaInput,
        submission: Submission,
        prepared: PreparedEvidence | None,
    ) -> None:
        self._state = "submitting"
        try:
            self._history.append(HistoryItem(test_input=current, submission=submission))
        except BaseException:
            self._state = "input_delivered"
            if prepared is not None:
                prepared.abort()
            raise
        if prepared is not None:
            prepared.commit(submission)
        self._current = None
        self._index += 1

    def _advance_after_submission(
        self, *, status: str, wait: bool
    ) -> TestReport | None:
        if status != "completed" and self._on_failure == "stop":
            self._stopped_early = True
            self._finish_runtime()
            return None
        if self._index < len(self._case.inputs):
            self._state = "ready"
            return None
        self._finish_runtime()
        if self._judge_enabled:
            return self._judge_locked(wait=wait)
        return None

    def cancel(self) -> None:
        """Cancel an unfinished Run and release its Evidence and runtime state."""

        with self._mutex:
            if self._state == "report_ready":
                return
            if self._state == "cancelled":
                return
            if self._state == "failed":
                raise InputProtocolError("A failed Run cannot be cancelled")
            if self._state not in {"ready", "input_delivered", "completed", "judging"}:
                raise InputProtocolError(
                    f"cancel() is not allowed while Run is {self._state}"
                )
            self._state = "cancelled"
            self._current = None
            if self._evidence is not None:
                self._evidence.cancel_step()
            self._runtime.close()

    def judge(self, *, wait: bool = True) -> TestReport:
        """Synchronously judge committed history for a completed Run.

        ``wait=False`` is rejected because the public Run API remains synchronous.
        The Official Provider waits on its bounded v2 operation internally. A
        Provider failure leaves the Run completed so the caller may retry without
        losing truthful history or pending-operation metadata.
        """

        with self._mutex:
            return self._judge_locked(wait=wait)

    def _judge_locked(self, *, wait: bool) -> TestReport:
        if not wait:
            raise ConfigurationError(
                "SDK v4 Judge requests are synchronous; wait must remain True"
            )
        if self._state == "report_ready" and self._report is not None:
            return self._report
        if self._state != "completed":
            raise InputProtocolError(
                f"judge() is not allowed while Run is {self._state}"
            )
        if self._judge_provider is None:
            raise ProviderError("No Judge Provider is configured")
        self._state = "judging"
        context = JudgeContext(
            case=self._case,
            history=tuple(self._history),
            run_status="completed",
            evidence_summary={
                "history_items": len(self._history),
                "stopped_early": self._stopped_early,
                "dropped_evidence_count": sum(
                    item.submission.dropped_count for item in self._history
                ),
            },
        )
        try:
            raw_report = self._judge_provider.judge(context)
            report = normalize_report(raw_report, run_id=self.run_id)
        except KumaError:
            self._state = "completed"
            raise
        except Exception as exc:
            self._state = "completed"
            raise ProviderError("The custom Judge Provider failed") from exc
        self._report = report
        self._state = "report_ready"
        return report

    def _finish_runtime(self) -> None:
        self._state = "completed"
        try:
            if self._evidence is not None:
                self._evidence.finish_run()
            self._runtime.close()
        except BaseException:
            self._state = "failed"
            raise


__all__ = ["Run", "RunState"]
