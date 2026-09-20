"""Validate bounded public detailed results, never infer a legacy assessment."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ProviderError
from ..evidence.assessment_contract import (
    ASSESSMENT_CONTRACT,
    assessment_json,
    valid_reference,
)
from ..repository.privacy import scan_sensitive_json
from ._official_wire import contains_private_fields, plain_json

_CONFIDENCE = ("low", "medium", "high")
_SEVERITY = ("none", "low", "medium", "high")
_AXES = {
    "task_completion": ("completed", "incomplete", "unverifiable", "not_assessed"),
    "artifact_quality": (
        "satisfactory",
        "defective",
        "unverifiable",
        "not_applicable",
        "not_assessed",
    ),
    "behavioral_integrity": (
        "no_anomaly_observed",
        "anomaly_observed",
        "unverifiable",
        "not_assessed",
    ),
}
_REASONS = (
    "observed_support",
    "observed_violation",
    "missing_evidence",
    "partial_capture",
    "source_not_attributable",
    "not_applicable",
    "not_assessed",
)
_CLAIM_REASONS = (
    "matched_evidence",
    "contradictory_evidence",
    "missing_evidence",
    "partial_capture",
    "source_not_attributable",
    "not_independently_verified",
)
_COVERAGE_REASONS = (
    "source_incomplete",
    "redacted",
    "unsupported_event",
    "unfinished_message",
    "claim_limit",
    "not_assessed",
)


def _closed(value: Any, fields: set[str]) -> bool:
    """Check exact public fields rather than retaining arbitrary nested objects."""
    return isinstance(value, Mapping) and set(value) == fields


def _references(value: Any, maximum: int = 16) -> bool:
    """Check bounded reference syntax; Core owns accepted-payload resolution."""
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(valid_reference(x) for x in value)
    )


def _codes(value: Any, allowed: tuple[str, ...], maximum: int) -> bool:
    """Require unique bounded stable codes without copying free-form diagnostics."""
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(isinstance(x, str) and x in allowed for x in value)
        and len(set(value)) == len(value)
    )


def _axis(value: Any, statuses: tuple[str, ...]) -> bool:
    """Validate one independent axis with confidence separate from severity."""
    return (
        _closed(
            value, {"status", "confidence", "severity", "evidence_refs", "reason_codes"}
        )
        and value["status"] in statuses
        and value["confidence"] in _CONFIDENCE
        and value["severity"] in _SEVERITY
        and _references(value["evidence_refs"])
        and _codes(value["reason_codes"], _REASONS, 8)
        and bool(value["reason_codes"])
        and (
            value["status"] in ("incomplete", "defective", "anomaly_observed")
            or value["severity"] == "none"
        )
    )


def _attribution(value: Any) -> bool:
    """Keep independent model/input/environment/conflict/unknown attribution."""
    return (
        _closed(value, {"cause", "confidence", "evidence_refs"})
        and value["cause"]
        in ("model", "input", "environment", "instruction_conflict", "unknown")
        and value["confidence"] in _CONFIDENCE
        and _references(value["evidence_refs"])
    )


def _claim(value: Any) -> bool:
    """Check bounded safe claim summaries and references, not semantic truth."""
    return (
        _closed(
            value,
            {
                "claim_id",
                "summary",
                "claim_type",
                "source",
                "verification",
                "confidence",
                "evidence_refs",
                "reason_code",
            },
        )
        and value["claim_type"]
        in ("execution", "delivery", "artifact_property", "reporting", "other")
        and isinstance(value["claim_id"], str)
        and 1 <= len(value["claim_id"]) <= 80
        and isinstance(value["summary"], str)
        and 1 <= len(value["summary"]) <= 1000
        and valid_reference(value["source"])
        and value["verification"] in ("supported", "contradicted", "unverifiable")
        and value["confidence"] in _CONFIDENCE
        and _references(value["evidence_refs"])
        and value["reason_code"] in _CLAIM_REASONS
        and _claim_witness_shape(value)
    )


def _claim_witness_shape(value: Mapping[str, Any]) -> bool:
    """Require witnesses for positive verification without pretending to resolve them.

    Core permits a reporting contradiction within the same public message, but
    not treating that self-report as execution proof. Unverifiable claims may
    retain context references without promoting them into independent witnesses.
    """
    verification = value["verification"]
    if verification == "unverifiable":
        return True
    expected = (
        "matched_evidence" if verification == "supported" else "contradictory_evidence"
    )
    reporting = value["claim_type"] == "reporting" and verification == "contradicted"
    return value["reason_code"] == expected and any(
        ref["pointer"] and (ref != value["source"] or reporting)
        for ref in value["evidence_refs"]
    )


def _coverage(value: Any) -> bool:
    """Make bounded/excluded claim processing visible rather than synthetic success."""
    return (
        _closed(value, {"status", "reason_codes", "reviewed_message_refs"})
        and value["status"] in ("complete", "partial", "unavailable")
        and _codes(value["reason_codes"], _COVERAGE_REASONS, 6)
        and _references(value["reviewed_message_refs"], 200)
        and (value["status"] != "complete" or not value["reason_codes"])
        and (value["status"] == "complete" or bool(value["reason_codes"]))
    )


def _valid(value: Any) -> bool:
    """Validate the complete closed independent-axis response shape."""
    return (
        _closed(
            value,
            {"schema_version", *_AXES, "attributions", "claims", "claim_coverage"},
        )
        and value["schema_version"] == ASSESSMENT_CONTRACT
        and all(_axis(value[name], statuses) for name, statuses in _AXES.items())
        and isinstance(value["attributions"], list)
        and len(value["attributions"]) <= 8
        and all(_attribution(x) for x in value["attributions"])
        and len({x["cause"] for x in value["attributions"]})
        == len(value["attributions"])
        and isinstance(value["claims"], list)
        and len(value["claims"]) <= 100
        and all(_claim(x) for x in value["claims"])
        and len({x["claim_id"] for x in value["claims"]}) == len(value["claims"])
        and _coverage(value["claim_coverage"])
    )


def validated_assessment(value: Any) -> dict[str, Any]:
    """Return only a closed bounded public assessment from a negotiated response.

    The Official Provider invokes this before report/pending success is committed.
    Unknown fields, private data, malformed references and oversize (>262144
    canonical UTF-8 bytes) fail with invalid_response. Core, not this shape check,
    resolves accepted Evidence and decides semantic support/contradiction.
    No missing legacy assessment is fabricated and no external data is fetched.
    """
    try:
        result = plain_json(value)
        valid = (
            _valid(result)
            and not contains_private_fields(result)
            and not scan_sensitive_json(result, location="assessment")
            and len(assessment_json(result)) <= 262144
        )
    except (ValueError, TypeError, RecursionError, UnicodeError):
        valid = False
    if not valid:
        raise ProviderError(
            "The service returned an invalid assessment", code="invalid_response"
        )
    return result


def extract_assessment(
    response: Mapping[str, Any], expected: str | None
) -> tuple[Mapping[str, Any], dict[str, Any] | None]:
    """Separate the negotiated assessment before legacy Judgment normalization.

    Expected comes only from prepared request metadata, not a response or newly
    fetched configuration. Missing opt-in or unexpected legacy fields reject
    before result persistence; legacy responses remain exactly unchanged.
    """
    if expected == ASSESSMENT_CONTRACT:
        assessment = validated_assessment(response.get("assessment"))
        return {
            key: value for key, value in response.items() if key != "assessment"
        }, assessment
    if expected is not None or "assessment" in response:
        raise ProviderError("Unexpected assessment", code="invalid_response")
    return response, None
