"""Validate public Judgment payloads without retaining private service fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..correlation import validate_run_receipt
from ..errors import ProviderError
from ._assessment_result import extract_assessment
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


def _report_extensions(
    response: Mapping[str, Any], excluded: set[str], receipt: Any, assessment: Any
) -> dict[str, Any]:
    """Combine legacy extensions with separately validated optional contracts.

    Called only after private-field and negotiated-schema checks. Absence stays
    absence; no assessment or receipt is synthesized from legacy status flags.
    """
    result = {
        str(key): plain_json(value)
        for key, value in response.items()
        if key not in excluded
    }
    if receipt is not None:
        result["run_receipt"] = receipt
    if assessment is not None:
        result["assessment"] = assessment
    return result


def _step_explanations(steps: list[Any]) -> list[dict[str, str]]:
    """Render validated non-pass verdicts without inventing a per-step cause.

    Args:
        steps: Public step_results already checked by _validated_collections.
            Only their validated step_id and verdict are used; issue strings
            remain server issue-ID references, not explanatory prose.

    Returns:
        One detached step_id/message pair per non-passed step, in server order.
        Messages are fixed English strings under 200 characters. Passed steps
        produce no entry. The list is local display interpretation, not a finding.

    Security/Privacy:
        Official normalization invokes this before report storage. Current
        assessment reasons/attributions refer to Evidence indexes and hashes,
        not step IDs; no trustworthy binding is available here or on recovery.
        Consequently global causes, pointers, summaries, extra fields and remote
        explanation text are never projected onto a step. No I/O or mutation
        occurs, and no capture gap or Agent fault is inferred from uncertainty.
    """
    messages = {
        "issue": "The Judge reported an issue for this step. See the referenced issue IDs for its findings.",
        "insufficient_evidence": (
            "The Judge reported an inconclusive outcome for this step. "
            "No reliably step-associated cause is available; this does not establish missing capture or an Agent defect."
        ),
    }
    return [
        {"step_id": step["step_id"], "message": messages[step["verdict"]]}
        for step in steps
        if step["verdict"] in messages
    ]


def _overall_explanation(status: str, assessment: Mapping[str, Any] | None) -> str:
    """Describe a validated non-pass report using only fixed global wording.

    Args:
        status: Already validated issue or insufficient_evidence status.
        assessment: Validated negotiated assessment, or None for legacy reports.

    Returns:
        A deterministic English summary under 1600 characters. Closed cause and
        axis reason codes select static sentences once each, never remote prose.

    Security/Privacy:
        Official normalization owns this local display projection before report
        persistence. Global Judge claims are not linked to steps or treated as
        independently verified capture failures. No bodies, references, paths,
        issue messages or private data are read; source fields are not mutated.
        Unsupported codes contribute nothing; existing schema validation still
        rejects malformed negotiated assessments before this helper is called.
    """
    sentences = [
        "The Judge reported an issue overall."
        if status == "issue"
        else "The Judge reported an inconclusive outcome overall."
    ]
    if assessment is not None:
        causes = {item["cause"] for item in assessment["attributions"]}
        for cause, sentence in (
            (
                "environment",
                "The Judge attributed a contributing cause to the environment.",
            ),
            ("input", "The Judge attributed a contributing cause to the input."),
            ("model", "The Judge attributed a contributing cause to the model."),
            (
                "instruction_conflict",
                "The Judge reported conflicting instructions as a contributing cause.",
            ),
            ("unknown", "The Judge reported an unknown cause."),
        ):
            if cause in causes:
                sentences.append(sentence)
        reasons = {
            code
            for axis in ("task_completion", "artifact_quality", "behavioral_integrity")
            for code in assessment[axis]["reason_codes"]
        }
        for reason, sentence in (
            (
                "missing_evidence",
                "The Judge reported missing evidence for its assessment.",
            ),
            (
                "partial_capture",
                "The Judge reported partial capture for its assessment.",
            ),
        ):
            if reason in reasons:
                sentences.append(sentence)
    sentences.append(
        "This summary is not step-specific and does not independently establish "
        "capture absence or an Agent defect; consult the original issues, assessment and evidence gaps."
    )
    return " ".join(sentences)


def _local_explanations(
    status: str, steps: list[Any], assessment: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return optional display extensions after Official response validation.

    Args:
        status: Validated overall verdict; pass and passed produce no fields.
        steps: Validated public step results, retained unchanged by the caller.
        assessment: Validated global assessment or None for legacy omission.

    Returns:
        Detached SDK-owned explanation fields for report storage and recovery.
        No step list is emitted when no non-passed step exists. This pure helper
        delegates static rendering without changing source findings or doing I/O.
    """
    if status in {"pass", "passed"}:
        return {}
    result: dict[str, Any] = {"explanation": _overall_explanation(status, assessment)}
    steps_text = _step_explanations(steps)
    if steps_text:
        result["step_explanations"] = steps_text
    return result


def normalize_official_judgment(
    response: Mapping[str, Any],
    *,
    expected_run_context: Mapping[str, Any] | None = None,
    allow_run_receipt: bool = False,
    run_id: str | None = None,
    case_id: str | None = None,
    operation_id: str | None = None,
    assessment_contract: str | None = None,
) -> Mapping[str, Any]:
    """Normalize a public Judgment, validating optional correlation separately.

    Official Provider passes the original negotiated context and public operation
    identity; durable recovery passes known Run/Case/operation IDs with explicit
    receipt permission. Missing expected or unexpected receipts fail before local
    success. Receipt metadata enters a closed extension, not Judge issue content.
    Ordinary callers omit all keywords and retain previous wire behavior.
    assessment_contract is the sealed request expectation, never inferred from
    response fields or refreshed config. Opt-in requires a validated assessment;
    legacy omission stays absent and never becomes three synthetic passes.
    Optional explanation/step_explanations are SDK-owned static interpretations,
    derived only after validation; remote fields with that name are not trusted.
    """

    response, assessment = extract_assessment(response, assessment_contract)
    receipt = None
    if "run_receipt" in response:
        if not allow_run_receipt and expected_run_context is None:
            raise ProviderError("Unexpected Run receipt", code="invalid_response")
        receipt = validate_run_receipt(
            response["run_receipt"],
            judgment_id=response.get("judgment_id"),
            expected=expected_run_context,
            run_id=run_id,
            case_id=case_id,
            operation_id=operation_id,
        )
        response = {
            key: value for key, value in response.items() if key != "run_receipt"
        }
    elif expected_run_context is not None:
        raise ProviderError(
            "The Backend omitted the negotiated Run receipt", code="invalid_response"
        )

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
    issues, step_results, evidence_gaps = _validated_collections(response)
    excluded = {
        "judgment_id",
        "status",
        "issues",
        "evidence_gaps",
        "stop_reason",
        "step_explanations",
        "explanation",
    }
    confidence = _validated_confidence(response.get("confidence"))
    if confidence is not None:
        excluded.add("confidence")
    result: dict[str, Any] = {
        "report_id": judgment_id,
        "status": "pass" if status == "passed" else status,
        "issues": issues,
        "evidence_gaps": evidence_gaps,
        "extensions": _report_extensions(response, excluded, receipt, assessment),
    }
    result["extensions"].update(_local_explanations(status, step_results, assessment))
    if confidence is not None:
        result["confidence"] = confidence
    if isinstance(response.get("stop_reason"), str):
        result["stop_reason"] = response["stop_reason"]
    return result


__all__ = ["normalize_official_judgment"]
