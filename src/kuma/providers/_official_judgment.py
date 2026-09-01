"""Validate public Judgment payloads without retaining private service fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ProviderError
from ._official_wire import (
    contains_private_fields,
    plain_json,
    required_text,
    valid_judgment_issue,
    valid_step_result,
)


def _validated_collections(
    response: Mapping[str, Any],
) -> tuple[list[Any], list[Any], list[Any]]:
    """Validate optional issue and step-result arrays without private fields."""
    issues = response.get("issues", [])
    step_results = response.get("step_results", [])
    evidence_gaps = response.get("evidence_gaps", [])
    flags = response.get("flags", {})
    mappings = (issues, step_results, evidence_gaps)
    if (
        any(not isinstance(value, list) for value in mappings)
        or any(
            not all(isinstance(item, Mapping) for item in value) for value in mappings
        )
        or not isinstance(flags, Mapping)
        or not all(
            isinstance(key, str) and isinstance(value, bool)
            for key, value in flags.items()
        )
        or not all(valid_step_result(item) for item in step_results)
        or not all(valid_judgment_issue(item) for item in issues)
    ):
        raise ProviderError(
            "The Backend returned an invalid Judgment", code="invalid_response"
        )
    return issues, step_results, evidence_gaps


def _validated_confidence(value: Any) -> str | int | float | None:
    """Accept bounded numeric confidence and legacy low/medium/high labels."""
    if value is None:
        return None
    if (isinstance(value, str) and value in {"low", "medium", "high"}) or (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= 1
    ):
        return value
    raise ProviderError(
        "The Backend returned invalid Judgment confidence",
        code="invalid_response",
    )


def normalize_official_judgment(
    response: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return the stable public report shape for one Backend Judgment."""

    if contains_private_fields(response):
        raise ProviderError(
            "The Backend returned private Judgment fields", code="invalid_response"
        )
    judgment_id = required_text(response.get("judgment_id"), "judgment_id")
    status = response.get("status")
    if status not in {"pass", "passed", "issue", "insufficient_evidence"}:
        raise ProviderError(
            "The Backend returned an invalid Judgment status", code="invalid_response"
        )
    issues, _step_results, evidence_gaps = _validated_collections(response)
    excluded = {
        "judgment_id",
        "status",
        "issues",
        "evidence_gaps",
        "stop_reason",
    }
    confidence = _validated_confidence(response.get("confidence"))
    if confidence is not None:
        excluded.add("confidence")
    result: dict[str, Any] = {
        "report_id": judgment_id,
        "status": "pass" if status == "passed" else status,
        "issues": issues,
        "evidence_gaps": evidence_gaps,
        "extensions": {
            str(key): plain_json(value)
            for key, value in response.items()
            if key not in excluded
        },
    }
    if confidence is not None:
        result["confidence"] = confidence
    if isinstance(response.get("stop_reason"), str):
        result["stop_reason"] = response["stop_reason"]
    return result


__all__ = ["normalize_official_judgment"]
