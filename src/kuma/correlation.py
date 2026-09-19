"""Closed public execution correlation, separate from semantic Judge verdicts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.validators import extend

from ._json_values import detach_json
from .errors import ProviderError, ValidationError
from .evidence.trace import _CAPTURE_REASONS
from .repository.privacy import scan_sensitive_json, scan_sensitive_text

RUN_CONTEXT_SCHEMA = "kuma.run_context.v1"
_EXTERNAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


def _strict_integer(checker: Any, value: Any) -> bool:
    """Reject booleans and integral floats in frozen strict-integer wire fields."""
    return type(value) is int


_Validator = extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _strict_integer),
)


def _text(maximum: int, *, nullable: bool = False) -> dict[str, Any]:
    """Describe a bounded opaque public label, allowing null only when explicit."""
    return {
        "type": ["string", "null"] if nullable else "string",
        "minLength": 1,
        "maxLength": maximum,
    }


def _closed(fields: dict[str, Any]) -> dict[str, Any]:
    """Require exactly the frozen fields; defaults and coercion are not allowed."""
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


_STEP = {
    "input_id": _text(128),
    "step_id": _text(80),
    "submission_id": _text(128),
    "external_run_id": {**_text(128, nullable=True), "pattern": _EXTERNAL_PATTERN},
    "external_invocation_id": {
        **_text(128, nullable=True),
        "pattern": _EXTERNAL_PATTERN,
    },
    "execution_status": {
        "enum": ["completed", "failed", "timeout", "aborted", "unknown"]
    },
    "client_step_elapsed_ms": {
        "type": ["integer", "null"],
        "minimum": 0,
        "maximum": 86_400_000,
    },
}
_COUNT = {"type": "integer", "minimum": 0}
_SUMMARY = _closed(
    {
        "schema_version": {"const": "defuzex.trace_capture_summary.v1"},
        "sampling_policy": {"const": "deterministic_topology_v1"},
        **{
            name: _COUNT
            for name in (
                "observed_spans",
                "retained_spans",
                "dropped_spans",
                "dropped_attributes_events",
                "observed_log_records",
                "retained_log_records",
                "dropped_log_records",
                "dropped_log_fields",
            )
        },
        "topology_complete": {"type": "boolean"},
    }
)
_CAPTURE = {
    "anyOf": [
        {"type": "null"},
        _closed(
            {
                "capture_status": {"enum": ["complete", "partial", "failed"]},
                "summary": _SUMMARY,
                "reasons": {
                    "type": "array",
                    "maxItems": 64,
                    "uniqueItems": True,
                    "items": {
                        "enum": sorted(
                            _CAPTURE_REASONS | {"trace_sensitive_content_redacted"}
                        )
                    },
                },
            }
        ),
    ]
}
_CONTEXT = _closed(
    {
        "schema_version": {"const": RUN_CONTEXT_SCHEMA},
        "run_id": _text(128),
        "case_id": _text(64),
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": _closed(_STEP),
        },
    }
)
_RECEIPT = _closed(
    {
        "schema_version": {"const": "kuma.run_receipt.v1"},
        "receipt_id": {"type": "string", "pattern": "^rr_[0-9a-f]{64}$"},
        "run_id": _text(128),
        "case_id": _text(64),
        "judgment_id": _text(64),
        "operation_id": _text(64, nullable=True),
        "judgment_reused": {"type": "boolean"},
        "source_judgment_id": _text(64),
        "source_evidence_ids": {
            "type": "array",
            "maxItems": 50,
            "uniqueItems": True,
            "items": _text(64),
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": _closed(
                {
                    **_STEP,
                    "evidence_status": {"const": "accepted"},
                    "trace_capture": _CAPTURE,
                }
            ),
        },
    }
)


def external_identifier(value: Any) -> str | None:
    """Validate optional caller correlation before any Run or transport side effect.

    None preserves omission compatibility. Values are labels, not credentials or
    authorization. Invalid/sensitive input raises value-free run_context_invalid.
    """
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or re.fullmatch(_EXTERNAL_PATTERN, value) is None
        or scan_sensitive_text(value, location="run_context")
    ):
        raise ValidationError(
            "External execution IDs must be safe ASCII labels of 1-128 characters",
            code="run_context_invalid",
        )
    return value


def _validated(value: Any, schema: dict[str, Any], *, remote: bool) -> dict[str, Any]:
    """Detach and validate bounded metadata without exposing validator diagnostics."""
    invalid = False
    try:
        result = detach_json(value)
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False).encode()
        invalid = len(encoded) > 131_072 or not _Validator(schema).is_valid(result)
        invalid = invalid or bool(scan_sensitive_json(result, location="run_context"))
    except Exception:
        invalid = True
    if invalid:
        if remote:
            raise ProviderError(
                "The Backend returned an invalid Run receipt", code="invalid_response"
            )
        raise ValidationError(
            "Run correlation metadata is invalid", code="run_context_invalid"
        )
    return result


def validate_run_context(value: Any) -> dict[str, Any]:
    """Return a detached closed Run context with unique submission identities.

    Called by Run/JudgeContext and Official upload before POST. This validates
    syntax and privacy, not caller honesty or server identity. Runtime Evidence
    coordinate binding is checked separately against the submitted history.
    """
    result = _validated(value, _CONTEXT, remote=False)
    if len({step["submission_id"] for step in result["steps"]}) != len(result["steps"]):
        raise ValidationError(
            "Run context submission identities must be unique",
            code="run_context_invalid",
        )
    return result


def _capture_consistent(capture: Any) -> bool:
    """Check receipt counters without inventing omitted span bodies or timing."""
    if capture is None:
        return True
    summary = capture["summary"]
    for observed, retained, dropped in (
        ("observed_spans", "retained_spans", "dropped_spans"),
        ("observed_log_records", "retained_log_records", "dropped_log_records"),
    ):
        if summary[observed] - summary[retained] != summary[dropped]:
            return False
    if summary["topology_complete"] != (
        "trace_topology_partial" not in capture["reasons"]
    ):
        return False
    if capture["capture_status"] == "failed" and summary["retained_spans"]:
        return False
    return capture["capture_status"] != "complete" or not (
        capture["reasons"]
        or summary["dropped_spans"]
        or summary["dropped_attributes_events"]
        or summary["dropped_log_records"]
        or summary["dropped_log_fields"]
    )


def validate_run_receipt(
    value: Any,
    *,
    judgment_id: str,
    expected: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    case_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Validate a server receipt independently of the existing Judgment contract.

    Args:
        value: Untrusted optional closed kuma.run_receipt.v1 object.
        judgment_id: Exact Judgment returned alongside this receipt.
        expected: Original context when available; every step value must echo it.
        run_id: Known Run identity, including durable recovery; None skips binding.
        case_id: Known Case identity; None skips binding.
        operation_id: Public Backend operation identity, or None for sync results.
    Returns:
        Detached public correlation without Evidence bodies or Core diagnostics.
    Raises:
        ProviderError: Malformed/private/mismatched result, before pending clear.
    Postconditions:
        Receipt source points to the immutable returned Judgment. Reuse does not
        assert current evidence was evaluated. Fresh-process recovery can bind
        persisted Run/Case/operation IDs but cannot reconstruct client timings or
        original external labels that were not persisted in the request ledger.
    """
    result = _validated(value, _RECEIPT, remote=True)
    valid = result["judgment_id"] == result["source_judgment_id"] == judgment_id
    valid &= result["operation_id"] == operation_id
    valid &= run_id is None or result["run_id"] == run_id
    valid &= case_id is None or result["case_id"] == case_id
    valid &= len({s["submission_id"] for s in result["steps"]}) == len(result["steps"])
    valid &= all(_capture_consistent(s["trace_capture"]) for s in result["steps"])
    if expected is not None:
        echoed = {
            "schema_version": RUN_CONTEXT_SCHEMA,
            "run_id": result["run_id"],
            "case_id": result["case_id"],
            "steps": [{key: step[key] for key in _STEP} for step in result["steps"]],
        }
        valid &= echoed == detach_json(expected)
    if not valid:
        raise ProviderError(
            "The Backend returned inconsistent Run correlation", code="invalid_response"
        )
    return result
