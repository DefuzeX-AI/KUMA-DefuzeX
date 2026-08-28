"""Immutable, JSON-compatible public contracts for the KUMA v4 API."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from .errors import KumaError, ValidationError


def _freeze_json(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item, name) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item, name) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValidationError(f"{name} must contain only finite JSON-compatible values")


def _require_schema_version(value: str, family: str) -> None:
    # Schema identifiers are a deployed cross-repository wire contract; the
    # Python package rename must not change their namespace.
    prefix = f"defuzex.{family}.v"
    if not value.startswith(prefix) or not value[len(prefix) :].isdigit():
        raise ValidationError(f"Invalid {family} schema version: {value!r}")
    if int(value[len(prefix) :]) != 1:
        raise ValidationError(
            f"Unsupported {family} schema major: {value!r}",
            code="schema_invalid",
        )


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValidationError(f"{name} must be a non-empty string")


def _require_optional_text(name: str, value: str | None) -> None:
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{name} must be text or None")


def _require_non_negative_integer(name: str, value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValidationError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class KumaInput:
    """One immutable Case input delivered by :meth:`Run.get_input`.

    ``payload`` and both mapping fields are recursively frozen after local
    JSON/schema validation so Provider-owned objects cannot mutate Run history.
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
            raise ValidationError("text input payload must be a string")
        if self.payload_type == "structured" and not isinstance(
            self.payload, (Mapping, list, tuple)
        ):
            raise ValidationError(
                "structured input payload must be a mapping or sequence"
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
    """Public metadata for one created, modified, deleted, or renamed path."""

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
    """Bounded file-change evidence for either container or local scope."""

    complete: bool
    scope: str
    changes: tuple[FileChange, ...] = ()
    errors: tuple[str, ...] = ()
    schema_version: str = "defuzex.file_evidence.v1"
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
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
    """Completeness and stable degradation reasons for one capture component."""

    status: str = "skipped"
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"complete", "partial", "failed", "skipped"}:
            raise ValidationError("Invalid capture component status")
        if any(not isinstance(item, str) for item in self.reasons):
            raise ValidationError("capture reasons must contain text values")
        object.__setattr__(self, "reasons", tuple(self.reasons))


@dataclass(frozen=True, slots=True)
class CaptureStatus:
    """Per-component Evidence status; partial capture is never hidden as success."""

    file_snapshot: CaptureComponent = field(default_factory=CaptureComponent)
    file_diff: CaptureComponent = field(default_factory=CaptureComponent)
    logs: CaptureComponent = field(default_factory=CaptureComponent)
    sensitive_scan: CaptureComponent = field(default_factory=CaptureComponent)
    traces: CaptureComponent = field(default_factory=CaptureComponent)

    def __post_init__(self) -> None:
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
    """One validated Agent result and its transactionally committed Evidence."""

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
    """

    inputs: tuple[KumaInput, ...]
    case_id: str | None = None
    input_type: str = "text"
    input_schema: Mapping[str, Any] | None = None
    rubric: Mapping[str, Any] | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
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
    """An Input/Submission pair whose Run, Case, and Input IDs must agree."""

    test_input: KumaInput
    submission: Submission

    def __post_init__(self) -> None:
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
    """Normalized final Judgment for one Run."""

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
    """One ordered result from the synchronous public batch Judge endpoint."""

    client_item_id: str
    run_id: str
    report: TestReport | None = None
    error: KumaError | None = None

    def __post_init__(self) -> None:
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
