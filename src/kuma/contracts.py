"""Immutable, JSON-compatible public contracts for the KUMA v4 API."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ._json_values import freeze_json
from .errors import KumaError, ValidationError


def _freeze_json(value: Any, name: str) -> Any:
    """Freeze a bounded public JSON field and map traversal failure safely.

    Args:
        value: Candidate contract field to detach and freeze.
        name: Constant public field label used in the safe error message.

    Returns:
        An immutable JSON graph with read-only mappings and tuple arrays.

    Raises:
        ValidationError: If the field is cyclic, exceeds the 256-container depth
            limit, contains non-finite/unsupported data, or a custom Mapping
            fails. Input payload and Submission output errors use stable code
            ``output_invalid``; other contract fields retain their prior code.

    Postconditions:
        Success shares no mutable JSON container with the caller. Failure does
        not include a key, value, object representation, or original exception.

    Side Effects:
        Iterates custom Mapping values locally and performs no external I/O.
    """
    try:
        return freeze_json(value)
    except Exception:
        code = "output_invalid" if name in {"payload", "output"} else None
        raise ValidationError(
            f"{name} must contain only finite JSON-compatible values",
            code=code,
        ) from None


def _require_schema_version(value: str, family: str) -> None:
    """Require the exact schema version for an immutable public contract."""
    prefix = f"defuzex.{family}.v"
    if not value.startswith(prefix) or not value[len(prefix) :].isdigit():
        raise ValidationError(f"Invalid {family} schema version: {value!r}")
    if int(value[len(prefix) :]) != 1:
        raise ValidationError(
            f"Unsupported {family} schema major: {value!r}",
            code="schema_invalid",
        )


def _require_identifier(name: str, value: str) -> None:
    """Require a non-empty bounded identifier without coercing external values."""
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValidationError(f"{name} must be a non-empty string")


def _require_optional_text(name: str, value: str | None) -> None:
    """Accept ``None`` or bounded text without coercing external values."""
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{name} must be text or None")


def _require_non_negative_integer(name: str, value: int | None) -> None:
    """Require a non-negative integer while rejecting booleans."""
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValidationError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class KumaInput:
    """One immutable Case input delivered by :meth:`Run.get_input`.

    ``payload`` and both mapping fields are recursively frozen after local
    JSON/schema validation so Provider-owned objects cannot mutate Run history.

    Attributes:
        run_id: Public identifier of the Run that owns this input.
        case_id: Public identifier of the Case that contains this input.
        input_id: Stable identifier used to correlate delivery, Submission, and
            Evidence for this one step.
        index: Zero-based position in the Case execution order.
        payload_type: ``"text"`` for a string payload or ``"structured"`` for
            a JSON object/array payload.
        payload: Agent-facing task value returned by ``Run.get_input``. It is a
            recursively immutable JSON-compatible value, may contain at most
            256 container levels, and cannot contain reference cycles. Shared
            acyclic children are detached and accepted.
        public_constraints: Public, non-secret constraints the Agent may use
            while handling this input.
        schema_version: Versioned wire family; currently
            ``"defuzex.input.v1"``.
        extensions: Forward-compatible public metadata. Unknown private grading
            fields are not accepted into official Cases.
    """

    run_id: str
    case_id: str
    input_id: str
    index: int
    payload_type: str
    payload: Any
    public_constraints: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "defuzex.input.v1"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate Input identity/type and freeze its payload and constraints."""
        _require_schema_version(self.schema_version, "input")
        for name in ("run_id", "case_id", "input_id"):
            _require_identifier(name, getattr(self, name))
        if (
            isinstance(self.index, bool)
            or not isinstance(self.index, int)
            or self.index < 0
        ):
            raise ValidationError("index must be zero or greater")
        if self.payload_type not in {"text", "structured"}:
            raise ValidationError("payload_type must be 'text' or 'structured'")
        if self.payload_type == "text" and not isinstance(self.payload, str):
            raise ValidationError(
                "text input payload must be a string", code="output_invalid"
            )
        if self.payload_type == "structured" and not isinstance(
            self.payload, (Mapping, list, tuple)
        ):
            raise ValidationError(
                "structured input payload must be a mapping or sequence",
                code="output_invalid",
            )
        object.__setattr__(self, "payload", _freeze_json(self.payload, "payload"))
        object.__setattr__(
            self,
            "public_constraints",
            _freeze_json(self.public_constraints, "public_constraints"),
        )
        object.__setattr__(
            self, "extensions", _freeze_json(self.extensions, "extensions")
        )


@dataclass(frozen=True, slots=True)
class FileChange:
    """Describe one bounded repository path change captured around a step.

    Attributes:
        path: Repository-relative POSIX-style path after boundary validation.
        change_type: One of ``created``, ``modified``, ``deleted``, or
            ``renamed``.
        file_type: Optional observed type: ``file``, ``directory``, ``symlink``,
            or ``special``.
        before_hash: SHA-256 hex digest before the step, when hashing completed.
        after_hash: SHA-256 hex digest after the step, when hashing completed.
        before_size: Non-negative byte size before the step, when available.
        after_size: Non-negative byte size after the step, when available.
        before_mode: Platform mode bits observed before the step.
        after_mode: Platform mode bits observed after the step.
        old_path: Previous repository-relative path for a detected rename.
        complete: ``False`` when limits or filesystem errors make the record
            incomplete; callers must inspect ``reason``.
        reason: Stable degradation reason, or ``None`` for complete capture.
        diff: Optional bounded text diff when ``upload_diff=True`` and the file
            is safe text. Binary and oversized content is never forced here.

    Security/Privacy:
        Paths are repository-relative. Hashes and metadata may be retained even
        when raw file text is excluded; ``diff`` remains subject to sensitive
        scanning and configured size limits.
    """

    path: str
    change_type: str
    file_type: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    before_size: int | None = None
    after_size: int | None = None
    before_mode: int | None = None
    after_mode: int | None = None
    old_path: str | None = None
    complete: bool = True
    reason: str | None = None
    diff: str | None = None

    def __post_init__(self) -> None:
        """Validate one file change and freeze its optional metadata."""
        _require_identifier("path", self.path)
        if self.change_type not in {"created", "modified", "deleted", "renamed"}:
            raise ValidationError("Unsupported file change type")
        if self.file_type is not None and self.file_type not in {
            "file",
            "directory",
            "symlink",
            "special",
        }:
            raise ValidationError("Unsupported file type")
        for name in ("before_size", "after_size", "before_mode", "after_mode"):
            _require_non_negative_integer(name, getattr(self, name))
        for name in ("before_hash", "after_hash", "old_path", "reason", "diff"):
            _require_optional_text(name, getattr(self, name))
        if not isinstance(self.complete, bool):
            raise ValidationError("complete must be a boolean")


@dataclass(frozen=True, slots=True)
class FileEvidence:
    """Group ordered file changes captured for one Submission.

    Attributes:
        complete: Whether the snapshot/diff process observed its entire bounded
            scope without omissions. ``False`` is never presented as success.
        scope: ``"container"`` for the supported isolated runtime or ``"local"``
            for an explicitly allowed local run.
        changes: Ordered immutable :class:`FileChange` records.
        errors: Stable non-sensitive capture reason codes; never raw tracebacks.
        schema_version: Versioned contract, currently
            ``"defuzex.file_evidence.v1"``.
        extensions: Bounded public extension metadata.
    """

    complete: bool
    scope: str
    changes: tuple[FileChange, ...] = ()
    errors: tuple[str, ...] = ()
    schema_version: str = "defuzex.file_evidence.v1"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze ordered file changes and enforce bounded snapshot metadata."""
        _require_schema_version(self.schema_version, "file_evidence")
        if not isinstance(self.complete, bool):
            raise ValidationError("complete must be a boolean")
        if self.scope not in {"container", "local"}:
            raise ValidationError("scope must be 'container' or 'local'")
        if any(not isinstance(item, FileChange) for item in self.changes):
            raise ValidationError("changes must contain FileChange values")
        if any(not isinstance(item, str) for item in self.errors):
            raise ValidationError("errors must contain text values")
        object.__setattr__(self, "changes", tuple(self.changes))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(
            self, "extensions", _freeze_json(self.extensions, "extensions")
        )


@dataclass(frozen=True, slots=True)
class CaptureComponent:
    """State whether one Evidence source was usable for a Submission.

    Attributes:
        status: ``complete``, ``partial``, ``failed``, or ``skipped``. ``partial``
            means some usable data exists; ``failed`` means capture attempted but
            produced no trustworthy component.
        reasons: Ordered stable reason codes explaining omissions or degradation.
            They are safe diagnostics, not raw provider/filesystem error text.
    """

    status: str = "skipped"
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate one capture status and freeze its reason codes."""
        if self.status not in {"complete", "partial", "failed", "skipped"}:
            raise ValidationError("Invalid capture component status")
        if any(not isinstance(item, str) for item in self.reasons):
            raise ValidationError("capture reasons must contain text values")
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    """Expose completeness independently for every SDK Evidence source.

    Attributes:
        file_snapshot: Baseline/final repository snapshot status.
        file_diff: Derived file-change comparison status.
        logs: Explicit file logs and mapped OTel log-record status.
        sensitive_scan: Privacy scanner status for uploadable Evidence.
        traces: In-process OpenTelemetry span capture status.

    A caller should use these fields together with ``Submission.missing`` and
    ``Submission.dropped_count``; an overall Submission status does not imply
    every Evidence component is complete.
    """

    file_snapshot: CaptureComponent = field(default_factory=CaptureComponent)
    file_diff: CaptureComponent = field(default_factory=CaptureComponent)
    logs: CaptureComponent = field(default_factory=CaptureComponent)
    sensitive_scan: CaptureComponent = field(default_factory=CaptureComponent)
    traces: CaptureComponent = field(default_factory=CaptureComponent)

    def __post_init__(self) -> None:
        """Require typed file, log, and Trace component statuses."""
        if any(
            not isinstance(component, CaptureComponent)
            for component in (
                self.file_snapshot,
                self.file_diff,
                self.logs,
                self.sensitive_scan,
                self.traces,
            )
        ):
            raise ValidationError(
                "capture status fields must be CaptureComponent values"
            )


@dataclass(frozen=True, slots=True)
class Submission:
    """Represent one committed Agent response and its correlated Evidence.

    Attributes:
        run_id: Run that owns this Submission.
        case_id: Case being executed by that Run.
        input_id: Exact delivered input answered by this Submission.
        status: ``completed``, ``failed``, ``timeout``, or ``aborted``.
        output: Finite JSON-compatible Agent result. Required for ``completed``;
            it may be passed explicitly or obtained from supported OTel output
            conventions before construction.
        error: Caller-supplied safe error summary for non-completed outcomes, or
            ``None``. Raw secrets and tracebacks must not be placed here.
        capture_status: Per-source completeness information.
        logs: Ordered bounded structured log segments retained for this step.
        file_evidence: Bounded file metadata/diff Evidence, or ``None`` when file
            tracking is disabled or unavailable.
        dropped_count: Number of Evidence records omitted because of limits,
            privacy rules, or capture failures.
        missing: Stable names/reasons for Evidence that could not be captured.
        schema_version: Versioned contract, currently
            ``"defuzex.submission.v1"``.
        extensions: Public typed Evidence and compatible extension payloads.

    Postconditions:
        Construction recursively freezes all JSON containers, so committed Run
        history cannot be changed through caller-owned objects.
    """

    run_id: str
    case_id: str
    input_id: str
    status: str
    output: Any = None
    error: str | None = None
    capture_status: CaptureStatus = field(default_factory=CaptureStatus)
    logs: tuple[Mapping[str, Any], ...] = ()
    file_evidence: FileEvidence | None = None
    dropped_count: int = 0
    missing: tuple[str, ...] = ()
    schema_version: str = "defuzex.submission.v1"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate Submission correlation/status and freeze all Evidence fields."""
        _require_schema_version(self.schema_version, "submission")
        for name in ("run_id", "case_id", "input_id"):
            _require_identifier(name, getattr(self, name))
        if self.status not in {"completed", "failed", "timeout", "aborted"}:
            raise ValidationError("Invalid submission status")
        _require_optional_text("error", self.error)
        if self.status == "completed" and self.output is None:
            raise ValidationError(
                "completed submissions require output", code="output_invalid"
            )
        if not isinstance(self.capture_status, CaptureStatus):
            raise ValidationError("capture_status must be a CaptureStatus")
        if self.file_evidence is not None and not isinstance(
            self.file_evidence, FileEvidence
        ):
            raise ValidationError("file_evidence must be FileEvidence or None")
        if any(not isinstance(item, Mapping) for item in self.logs):
            raise ValidationError("logs must contain mappings")
        if isinstance(self.dropped_count, bool) or not isinstance(
            self.dropped_count, int
        ):
            raise ValidationError("dropped_count must be an integer")
        if self.dropped_count < 0:
            raise ValidationError("dropped_count must be zero or greater")
        if any(not isinstance(item, str) for item in self.missing):
            raise ValidationError("missing must contain text values")
        object.__setattr__(self, "output", _freeze_json(self.output, "output"))
        object.__setattr__(
            self, "logs", tuple(_freeze_json(item, "logs") for item in self.logs)
        )
        object.__setattr__(self, "missing", tuple(self.missing))
        object.__setattr__(
            self, "extensions", _freeze_json(self.extensions, "extensions")
        )


@dataclass(frozen=True, slots=True)
class Case:
    """A complete normalized input sequence and optional public custom rubric.

    Official Cases carry integrity references in ``extensions`` and never expose
    a private server-side rubric through this contract.

    Attributes:
        inputs: Non-empty ordered immutable sequence delivered by the Run.
        case_id: Public Case identifier, or ``None`` only before normalization of
            a custom provider result.
        input_type: Shared payload kind for every input: ``text`` or
            ``structured``.
        input_schema: Read-only JSON Schema for structured inputs, otherwise
            usually ``None``.
        rubric: Transparent public rules for a local custom Judge, or ``None``.
            It may describe criteria but must not contain expected outputs,
            answer keys, hidden answers, or official private-rubric content.
        extensions: Public metadata and, for official Cases, validated opaque
            provenance needed to submit the correct ``case_id`` for judging.

    Security/Privacy:
        ``extensions`` is validated at official boundaries and must not contain
        private rubric criteria, hidden answers, prompts, or provider keys.
    """

    inputs: tuple[KumaInput, ...]
    case_id: str | None = None
    input_type: str = "text"
    input_schema: Mapping[str, Any] | None = None
    rubric: Mapping[str, Any] | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate Case identity and Input schema while freezing public extensions."""
        if not self.inputs:
            raise ValidationError("A Case must contain at least one input")
        if any(not isinstance(item, KumaInput) for item in self.inputs):
            raise ValidationError("Case inputs must contain KumaInput values")
        if self.case_id is not None:
            _require_identifier("case_id", self.case_id)
        if self.input_type not in {"text", "structured"}:
            raise ValidationError("input_type must be 'text' or 'structured'")
        if any(item.payload_type != self.input_type for item in self.inputs):
            raise ValidationError("Case input_type must match every Input payload_type")
        if self.case_id is not None and any(
            item.case_id != self.case_id for item in self.inputs
        ):
            raise ValidationError("Case case_id must match every Input")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(
            self, "input_schema", _freeze_json(self.input_schema, "input_schema")
        )
        object.__setattr__(self, "rubric", _freeze_json(self.rubric, "rubric"))
        object.__setattr__(
            self, "extensions", _freeze_json(self.extensions, "extensions")
        )


@dataclass(frozen=True, slots=True)
class HistoryItem:
    """Pair one delivered input with the only Submission committed for it.

    Attributes:
        test_input: Immutable input previously returned by ``Run.get_input``.
        submission: Immutable response committed by ``Run.submit``.

    The constructor rejects mismatched Run, Case, or Input identifiers so a
    Judge never receives cross-run Evidence through normal SDK history.
    """

    test_input: KumaInput
    submission: Submission

    def __post_init__(self) -> None:
        """Require a Submission associated with the paired Run Input."""
        if not isinstance(self.test_input, KumaInput) or not isinstance(
            self.submission, Submission
        ):
            raise ValidationError("HistoryItem requires an Input and Submission")
        if self.test_input.run_id != self.submission.run_id:
            raise ValidationError("HistoryItem run_id values must match")
        if self.test_input.case_id != self.submission.case_id:
            raise ValidationError("HistoryItem case_id values must match")
        if self.test_input.input_id != self.submission.input_id:
            raise ValidationError("HistoryItem input_id values must match")


@dataclass(frozen=True, slots=True)
class TestReport:
    """Expose the normalized public Judgment returned for one completed Run.

    Attributes:
        report_id: Public identifier of this Judgment.
        run_id: Run whose committed history was judged.
        status: ``pass``, ``issue``, or ``insufficient_evidence``. The last value
            means the Judge could not safely reach a behavior conclusion.
        confidence: Optional ``low``/``medium``/``high`` label or finite numeric
            confidence from ``0`` through ``1``.
        stop_reason: Stable public reason the judging lifecycle ended, such as
            ``case_completed``.
        issues: Ordered immutable public issue objects. Their exact fields are
            service-versioned and must be treated as JSON data.
        evidence_gaps: Ordered public explanations of missing Evidence.
        schema_version: Versioned report family, currently
            ``"defuzex.report.v1"``.
        extensions: Additional validated public report metadata.

    Security/Privacy:
        The report does not expose the private rubric, prompt, hidden answer,
        model configuration, or raw provider response.
    """

    report_id: str
    run_id: str
    status: str
    confidence: float | Literal["low", "medium", "high"] | None = None
    stop_reason: str = "case_completed"
    issues: tuple[Mapping[str, Any], ...] = ()
    evidence_gaps: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = "defuzex.report.v1"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate Judgment score/status and freeze issues, steps, and extensions."""
        _require_schema_version(self.schema_version, "report")
        _require_identifier("report_id", self.report_id)
        _require_identifier("run_id", self.run_id)
        if self.status not in {"pass", "issue", "insufficient_evidence"}:
            raise ValidationError("Invalid report status")
        numeric_confidence = isinstance(
            self.confidence, int | float
        ) and not isinstance(self.confidence, bool)
        labeled_confidence = isinstance(self.confidence, str) and self.confidence in {
            "low",
            "medium",
            "high",
        }
        if self.confidence is not None and not (
            labeled_confidence
            or (
                numeric_confidence
                and math.isfinite(self.confidence)
                and 0 <= self.confidence <= 1
            )
        ):
            raise ValidationError(
                "confidence must be low, medium, high, or a finite number between 0 and 1"
            )
        _require_identifier("stop_reason", self.stop_reason)
        if any(not isinstance(item, Mapping) for item in self.issues):
            raise ValidationError("report issues must be mappings")
        if any(not isinstance(item, Mapping) for item in self.evidence_gaps):
            raise ValidationError("report evidence_gaps must be mappings")
        object.__setattr__(
            self, "issues", tuple(_freeze_json(item, "issues") for item in self.issues)
        )
        object.__setattr__(
            self,
            "evidence_gaps",
            tuple(_freeze_json(item, "evidence_gaps") for item in self.evidence_gaps),
        )
        object.__setattr__(
            self, "extensions", _freeze_json(self.extensions, "extensions")
        )


@dataclass(frozen=True, slots=True)
class JudgeBatchResult:
    """Represent one item in an ordered batch Judge response.

    Attributes:
        client_item_id: Caller-visible correlation identifier from the matching
            batch request item.
        run_id: Run associated with this result.
        report: Normalized public report on success, otherwise ``None``.
        error: Stable public SDK error on failure, otherwise ``None``.

    Exactly one of ``report`` and ``error`` is present, allowing callers to
    handle partial batch failure without treating failed items as successes.
    """

    client_item_id: str
    run_id: str
    report: TestReport | None = None
    error: KumaError | None = None

    def __post_init__(self) -> None:
        """Require exactly one normalized report or public error for a batch item."""
        _require_identifier("client_item_id", self.client_item_id)
        _require_identifier("run_id", self.run_id)
        if (self.report is None) == (self.error is None):
            raise ValidationError(
                "JudgeBatchResult requires exactly one of report or error"
            )
        if self.report is not None and self.report.run_id != self.run_id:
            raise ValidationError("Batch Judgment report run_id must match")
        if self.error is not None and not isinstance(self.error, KumaError):
            raise ValidationError("Batch Judgment error must be a KumaError")


__all__ = [
    "CaptureComponent",
    "CaptureStatus",
    "Case",
    "FileChange",
    "FileEvidence",
    "HistoryItem",
    "JudgeBatchResult",
    "KumaInput",
    "Submission",
    "TestReport",
]
