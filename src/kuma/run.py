"""The single strict-handshake Run state machine shared by all Providers."""

from __future__ import annotations

import os
import threading
from collections.abc import Sequence
from typing import Any, Literal

from ._json_values import detach_json
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
    """Return one detached bounded JSON graph for internal Run callers.

    Args:
        value: Candidate Input or Submission value.

    Returns:
        A mutable built-in JSON graph detached from caller-owned containers.

    Raises:
        JsonStructureError: Through :func:`detach_json` when the graph is
            unsupported, cyclic, non-finite, or deeper than 256 containers.

    Postconditions:
        Success is safe for later immutable contract construction.

    Side Effects:
        Traverses custom Mapping values locally and performs no external I/O.
    """
    return detach_json(value)


def _validate_json(value: Any, description: str) -> Any:
    """Validate Agent output before Evidence, persistence, Judge, or billing.

    Args:
        value: Explicit or OTel-derived Agent result to detach. Cyclic graphs and
            values deeper than 256 nested containers are invalid.
        description: Safe field label used in the stable public error message;
            callers pass a constant rather than user-controlled text.

    Returns:
        A detached finite JSON value made of built-in dictionaries/lists and
        scalars. Shared aliases are copied and remain valid.

    Raises:
        ValidationError: If traversal, a custom Mapping, or JSON encoding fails.
            The stable code is ``output_invalid`` and the original exception or
            caller value is never included in display text.

    Preconditions:
        Run state has not entered Evidence preparation or submission commit.

    Postconditions:
        Success is safe to freeze into Submission history. Failure leaves the
        current Input delivered and performs no persistence or network action.

    Side Effects:
        Iterates custom Mapping output locally; performs no external I/O.

    Security/Privacy:
        Arbitrary Mapping errors, recursion details, keys, values, and object
        representations are replaced with one stable SDK message.
    """
    try:
        return _plain_json(value)
    except Exception:
        raise ValidationError(
            f"{description} must be JSON serializable", code="output_invalid"
        ) from None


def _validate_log_paths(
    logs: Sequence[str | os.PathLike[str]] | None,
) -> tuple[str, ...] | None:
    """Validate explicit log-path syntax before Run state or Evidence I/O.

    Args:
        logs: ``None`` or an ordered sequence whose elements are text or
            ``os.PathLike`` values resolving to text. A bare string and all
            bytes-like containers are values, not path sequences, and are
            rejected.

    Returns:
        ``None`` when capture was not requested; otherwise an immutable tuple
        of detached path spellings for repository-root resolution.

    Raises:
        ValidationError: If the top-level value is not an ordered sequence or
            any element is not a text path-like value.

    Preconditions:
        None. Validation deliberately runs before checking Run state so invalid
        path containers cannot reach Evidence preparation or Judge transport.

    Postconditions:
        Success has not read a path, changed Run state, or performed network I/O.

    Security/Privacy:
        Conversion failures use one stable message and never include the
        supplied object, path spelling, or underlying exception text.
    """
    if logs is None:
        return None
    if isinstance(logs, (str, bytes, bytearray)) or not isinstance(logs, Sequence):
        raise ValidationError(
            "logs must be None or an ordered sequence of text paths",
            code="logs_invalid",
        )
    normalized: list[str] = []
    for item in logs:
        if isinstance(item, (bytes, bytearray)) or not isinstance(
            item, (str, os.PathLike)
        ):
            raise ValidationError(
                "logs must be None or an ordered sequence of text paths",
                code="logs_invalid",
            )
        try:
            value = os.fspath(item)
        except Exception:
            raise ValidationError(
                "logs must be None or an ordered sequence of text paths",
                code="logs_invalid",
            ) from None
        if not isinstance(value, str):
            raise ValidationError(
                "logs must be None or an ordered sequence of text paths",
                code="logs_invalid",
            )
        normalized.append(value)
    return tuple(normalized)


class Run:
    """Coordinate the synchronous Input → Submission → Judge lifecycle.

    A Run is created by :func:`kuma.create_run`; callers do not instantiate it
    directly. It owns exactly one immutable Case, one active runtime lease, the
    ordered committed history, transactional Evidence state, and at most one
    final report. Public methods are protected by a re-entrant lock so concurrent
    calls cannot commit the same input twice.

    Attributes:
        run_id: Public identifier for this execution.
        case_id: Public Case identifier used by official Judge requests.
        strategy: Requested/selected public Case strategy identifier.
        max_steps: Actual number of inputs in this validated Case. This is not
            the configured upper bound when the provider returned fewer steps.
    """

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
        """Initialize one validated Run and take ownership of its runtime lease.

        Args:
            run_id: Newly generated public Run identifier.
            case: Fully normalized immutable Case owned by this Run.
            runtime: Open runtime session whose lock/resources this Run closes.
            judge_provider: Configured Judge provider, or ``None`` when judging
                is disabled or deliberately unavailable.
            judge_enabled: Whether the last Submission should invoke Judge.
            on_failure: ``continue`` or ``stop`` behavior after a non-completed
                Submission.
            strategy: Public Case strategy recorded for introspection.
            evidence: Step Evidence collector, or ``None`` when capture is not
                configured.

        Preconditions:
            ``case`` has at least one correlated input and ``runtime`` is open
            with the active-Run lease already acquired.

        Postconditions:
            The Run is ``ready`` at input index zero and owns the supplied
            runtime and Evidence lifecycle until completion or cancellation.

        Side Effects:
            None beyond taking ownership of already-created objects.
        """
        self.run_id = run_id
        self.case_id = case.case_id or ""
        self.strategy = strategy
        self.max_steps = len(case.inputs)
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

        Args:
            full: Return the immutable :class:`KumaInput` when ``True``; return
                only its JSON-compatible payload when ``False``.

        Returns:
            Current payload or ``KumaInput``. Returns ``None`` after all inputs
            are committed. Repeated calls before :meth:`submit` return the same
            input and do not advance the Run.

        Raises:
            InputProtocolError: If the Run cannot currently deliver an input.
            EvidenceCaptureError: If step Evidence initialization fails.

        Preconditions:
            The Run is ``ready`` or already ``input_delivered``. The caller must
            submit the delivered input before requesting the next one.

        Postconditions:
            First delivery changes ``ready`` to ``input_delivered`` and starts
            that input's Evidence transaction. Repeated delivery changes
            nothing. Returning ``None`` means no uncommitted inputs remain.

        Side Effects:
            May initialize bounded file, log, and Trace capture. It never calls
            Judge or appends history.

        Security/Privacy:
            Only the public Case input is returned; private Rubrics and private
            evaluation metadata are never exposed.
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
        logs: Sequence[str | os.PathLike[str]] | None = None,
        wait: bool = True,
    ) -> TestReport | None:
        """Validate and commit one result, then advance or judge synchronously.

        Args:
            output: Finite JSON-compatible Agent result. When omitted for a
                completed Submission, KUMA uses a supported OTel
                ``invoke_agent``/``invoke_workflow`` output. Explicit output has
                priority; ``None`` is invalid for ``status="completed"``.
                Containers may nest up to 256 levels (root container is level
                one). Cycles are invalid; shared acyclic children are allowed.
            status: ``completed``, ``failed``, ``timeout``, or ``aborted``.
            error: Optional caller-safe summary for a non-completed Submission.
                Do not pass secrets or raw tracebacks.
            logs: ``None`` or an ordered sequence of text/path-like log-file
                paths. Relative paths resolve from this Run's repository, never
                the process working directory. Bare ``str``/bytes-like values
                are invalid. Accepted files remain subject to count/size,
                repository-scope, suffix, symlink, and sensitive-data checks.
            wait: Must remain ``True`` when the last Submission triggers Judge;
                the public Run API exposes synchronous completion only.

        Returns:
            Final :class:`TestReport` only when this is the last input and Judge
            completes; otherwise ``None``. The committed result is always
            available through :attr:`history`.

        Raises:
            InputProtocolError: If no input is currently delivered.
            ValidationError: If output, status, error, or serialization is invalid.
            EvidenceCaptureError: If requested Evidence cannot be captured safely.
            KumaError: If the final official or custom Judge fails.

        Preconditions:
            Exactly one input has been delivered and not yet submitted. A
            completed Submission needs explicit output or a supported OTel final
            output. Log paths must be in the allowed Evidence scope.

        Postconditions:
            Success appends exactly one immutable history item and commits
            Evidence offsets, local records, and Trace budgets together. The Run
            becomes ``ready``, ``completed``, or ``report_ready``. Validation or
            preparation failure leaves the input delivered. Judge failure leaves
            completed history available for retry.

        Side Effects:
            Reads bounded log/file state, may atomically write local Evidence,
            and may synchronously poll the configured Judge after the final input.

        Security/Privacy:
            Output, error, logs, diffs, and Evidence may cross the public Judge
            boundary. Do not pass credentials, raw tracebacks, prompts, private
            rubrics, or unauthorized repository content.
        """

        log_paths = _validate_log_paths(logs)
        with self._mutex:
            current = self._submission_input(log_paths)
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
                logs=log_paths,
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

    def _submission_input(self, logs: Sequence[str] | None) -> KumaInput:
        """Require one delivered Input and an Evidence collector when logs are supplied."""
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
        """Validate status/output and fall back to actual OTel Agent output when omitted."""
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
        logs: Sequence[str] | None,
    ) -> PreparedEvidence | None:
        """Prepare transactional Evidence without advancing collector offsets."""
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
        """Build the immutable Submission and abort prepared Evidence on validation failure."""
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
        """Append history then commit Evidence, restoring deliverable state on append failure."""
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
        """Advance to the next Input, stop early, or enter the configured Judge."""
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
        """Cancel an unfinished Run and release Evidence and its runtime lease.

        Returns:
            ``None``.

        Raises:
            InputProtocolError: If the Run is failed or is in an intermediate
                state whose work must not be hidden by cancellation.

        Preconditions:
            The Run is in a cancellable public lifecycle state.

        Postconditions:
            An unfinished Run becomes ``cancelled``; active Evidence is discarded
            and runtime resources are released. Calls on ``cancelled`` or
            ``report_ready`` Runs are idempotent.

        Side Effects:
            Closes runtime resources and removes validated temporary runtime
            files. It does not create a Submission or invoke Judge.
        """

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

        Args:
            wait: Must be ``True``. Official operation polling remains internal
                and is bounded by ``operation_wait_timeout`` from ``create_run``.

        Returns:
            Validated final :class:`TestReport`. Repeated calls after success
            return the same report object.

        Raises:
            ConfigurationError: If ``wait=False`` is requested.
            InputProtocolError: If the Run is not completed and ready for Judge.
            ProviderError: If no Judge is configured or a custom Judge fails.
            KumaError: If an official Judge operation fails or times out.

        Preconditions:
            Every delivered input has a committed Submission, state is
            ``completed``, and a Judge Provider is configured.

        Postconditions:
            Success stores the report and changes state to ``report_ready``.
            Failure restores ``completed`` so retry reuses the same immutable
            history, idempotency identity, and pending operation.

        Side Effects:
            Calls the configured Judge for an unresolved report. Official Judge
            uses bounded synchronous HTTP polling.

        Security/Privacy:
            Only validated public provenance, committed Submission Evidence, and
            bounded public metadata cross the official Backend boundary. The SDK
            never retrieves a private rubric.
        """

        with self._mutex:
            return self._judge_locked(wait=wait)

    def _judge_locked(self, *, wait: bool) -> TestReport:
        """Judge completed history once and restore ``completed`` after Provider failure."""
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
        """Seal Evidence and release the active-Run lease, marking cleanup failure."""
        self._state = "completed"
        try:
            if self._evidence is not None:
                self._evidence.finish_run()
            self._runtime.close()
        except BaseException:
            self._state = "failed"
            raise


__all__ = ["Run", "RunState"]
