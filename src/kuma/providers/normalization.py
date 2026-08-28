"""Normalize flexible custom Provider results into stable v4 contracts."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts import Case, KumaInput, TestReport
from ..errors import ProviderError, ValidationError
from ..repository.privacy import PRIVATE_DATA_FIELDS, contains_private_data
from ..repository.requirements import validate_schema, validate_structured_input

_CASE_FIELDS = frozenset(
    {"case_id", "inputs", "input_type", "input_schema", "rubric", "extensions"}
)
_INPUT_FIELDS = frozenset(
    {"input_id", "payload", "payload_type", "public_constraints", "extensions"}
)
_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "report_id",
        "run_id",
        "status",
        "confidence",
        "stop_reason",
        "issues",
        "evidence_gaps",
        "extensions",
    }
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(child) for child in value]
    return value


def _ensure_json(value: Any, description: str) -> None:
    try:
        json.dumps(_plain_json(value), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProviderError(f"{description} must be JSON serializable") from exc


def _bounded_inputs(value: Any, max_inputs: int) -> list[Any]:
    if isinstance(value, (str, bytes, Mapping, KumaInput)):
        return [value]
    if not isinstance(value, Iterable):
        raise ProviderError("Case Provider must return a Case or iterable Inputs")
    iterator = iter(value)
    inputs: list[Any] = []
    for item in iterator:
        inputs.append(item)
        if len(inputs) > max_inputs:
            raise ProviderError("Case Provider returned more than max_inputs")
    return inputs


def _input_parts(
    value: Any,
) -> tuple[Any, str, str | None, Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, KumaInput):
        return (
            _plain_json(value.payload),
            value.payload_type,
            value.input_id,
            value.public_constraints,
            value.extensions,
        )
    if isinstance(value, str):
        return value, "text", None, {}, {}
    if isinstance(value, Mapping) and "payload" in value and "payload_type" in value:
        unknown = set(value) - _INPUT_FIELDS
        supplied_extensions = value.get("extensions", {})
        constraints = value.get("public_constraints", {})
        if not isinstance(constraints, Mapping) or not isinstance(
            supplied_extensions, Mapping
        ):
            raise ProviderError("Input constraints and extensions must be mappings")
        extensions = dict(supplied_extensions)
        extensions.update({str(key): value[key] for key in unknown})
        return (
            value["payload"],
            str(value["payload_type"]),
            value.get("input_id"),
            constraints,
            extensions,
        )
    if isinstance(value, (Mapping, list, tuple)):
        return _plain_json(value), "structured", None, {}, {}
    raise ProviderError("Each Input must be text, structured JSON, or KumaInput")


@dataclass(frozen=True, slots=True)
class _CaseParts:
    inputs: Any
    case_id: str | None = None
    input_type: str | None = None
    input_schema: Mapping[str, Any] | None = None
    rubric: Mapping[str, Any] | None = None
    extensions: Mapping[str, Any] | None = None


def _case_parts(result: Any) -> _CaseParts:
    if isinstance(result, Case):
        return _CaseParts(
            inputs=result.inputs,
            case_id=result.case_id,
            input_type=result.input_type,
            input_schema=result.input_schema,
            rubric=result.rubric,
            extensions=result.extensions,
        )
    if not isinstance(result, Mapping) or "inputs" not in result:
        return _CaseParts(inputs=result)

    private = {str(key) for key in result if str(key).casefold() in PRIVATE_DATA_FIELDS}
    if private:
        fields = ", ".join(sorted(private))
        raise ProviderError(f"Custom Case contains prohibited private fields: {fields}")
    public_case = {key: value for key, value in result.items() if key != "rubric"}
    if contains_private_data(public_case):
        raise ProviderError("Custom Case contains nested private fields")
    supplied_extensions = result.get("extensions", {})
    if not isinstance(supplied_extensions, Mapping):
        raise ProviderError("Case extensions must be a mapping")
    extensions = dict(supplied_extensions)
    extensions.update({str(key): result[key] for key in set(result) - _CASE_FIELDS})
    return _CaseParts(
        inputs=result["inputs"],
        case_id=result.get("case_id"),
        input_type=result.get("input_type"),
        input_schema=result.get("input_schema"),
        rubric=result.get("rubric"),
        extensions=extensions,
    )


def _resolved_input_type(
    parsed: list[tuple[Any, str, str | None, Mapping[str, Any], Mapping[str, Any]]],
    *,
    declared: str | None,
    required: str | None,
) -> str:
    inferred_types = {item[1] for item in parsed}
    if len(inferred_types) != 1:
        raise ProviderError("A Case cannot mix text and structured Inputs")
    inferred = next(iter(inferred_types))
    resolved = required or declared or inferred
    if resolved not in {"text", "structured"}:
        raise ProviderError("Case input_type must be 'text' or 'structured'")
    if declared is not None and declared != resolved:
        raise ProviderError("Case input_type conflicts with the accepted requirement")
    if inferred != resolved:
        raise ProviderError("Case Input payloads conflict with input_type")
    return resolved


def _resolved_input_schema(
    *,
    declared: Mapping[str, Any] | None,
    required: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    resolved = required if required is not None else declared
    if (
        required is not None
        and declared is not None
        and _plain_json(required) != _plain_json(declared)
    ):
        raise ProviderError("Case input_schema conflicts with the accepted requirement")
    if resolved is not None:
        try:
            validate_schema(resolved)
        except ValidationError as exc:
            raise ProviderError(
                "Case Provider returned an invalid input_schema"
            ) from exc
    return resolved


def _validate_structured_payloads(
    parsed: list[tuple[Any, str, str | None, Mapping[str, Any], Mapping[str, Any]]],
    schema: Mapping[str, Any] | None,
) -> None:
    if schema is None:
        return
    for payload, *_ in parsed:
        try:
            validate_structured_input(payload, schema)
        except ValidationError as exc:
            raise ProviderError(
                "Case Provider returned an Input that violates input_schema"
            ) from exc


def _normalized_inputs(
    parsed: list[tuple[Any, str, str | None, Mapping[str, Any], Mapping[str, Any]]],
    *,
    run_id: str,
    case_id: str,
) -> tuple[KumaInput, ...]:
    inputs: list[KumaInput] = []
    input_ids: set[str] = set()
    for index, parts in enumerate(parsed):
        payload, payload_type, input_id, constraints, extensions = parts
        resolved_input_id = input_id or _new_id("input")
        if not isinstance(resolved_input_id, str) or not resolved_input_id.strip():
            raise ProviderError("input_id must be a non-empty string")
        if resolved_input_id in input_ids:
            raise ProviderError("Case Provider returned duplicate input_id values")
        input_ids.add(resolved_input_id)
        _ensure_json(payload, "Input payload")
        _ensure_json(constraints, "Input public_constraints")
        _ensure_json(extensions, "Input extensions")
        try:
            inputs.append(
                KumaInput(
                    run_id=run_id,
                    case_id=case_id,
                    input_id=resolved_input_id,
                    index=index,
                    payload_type=payload_type,
                    payload=payload,
                    public_constraints=constraints,
                    extensions=extensions,
                )
            )
        except ValidationError as exc:
            raise ProviderError("Case Provider returned an invalid Input") from exc
    return tuple(inputs)


def normalize_case(
    result: Any,
    *,
    run_id: str,
    max_inputs: int,
    required_input_type: str | None,
    required_input_schema: Mapping[str, Any] | None,
) -> Case:
    """Normalize one complete custom Case before exposing any Input."""

    if max_inputs <= 0:
        raise ProviderError("max_inputs must be positive")
    parts = _case_parts(result)
    if parts.case_id is not None and (
        not isinstance(parts.case_id, str) or not parts.case_id.strip()
    ):
        raise ProviderError("case_id must be a non-empty string")
    resolved_case_id = parts.case_id or _new_id("case")
    if parts.rubric is not None and not isinstance(parts.rubric, Mapping):
        raise ProviderError("Custom rubric must be a mapping")
    if parts.input_schema is not None and not isinstance(parts.input_schema, Mapping):
        raise ProviderError("Case input_schema must be a mapping")

    raw_sequence = _bounded_inputs(parts.inputs, max_inputs)
    if not raw_sequence:
        raise ProviderError("Case Provider returned no Inputs")
    parsed = [_input_parts(value) for value in raw_sequence]
    resolved_type = _resolved_input_type(
        parsed,
        declared=parts.input_type,
        required=required_input_type,
    )
    resolved_schema = _resolved_input_schema(
        declared=parts.input_schema,
        required=required_input_schema,
    )
    if resolved_type == "structured" and resolved_schema is not None:
        _validate_structured_payloads(parsed, resolved_schema)
    inputs = _normalized_inputs(parsed, run_id=run_id, case_id=resolved_case_id)

    extensions = parts.extensions or {}
    _ensure_json(parts.rubric, "Custom rubric")
    _ensure_json(extensions, "Case extensions")
    return Case(
        inputs=inputs,
        case_id=resolved_case_id,
        input_type=resolved_type,
        input_schema=resolved_schema,
        rubric=parts.rubric,
        extensions=extensions,
    )


def normalize_report(result: Any, *, run_id: str) -> TestReport:
    if isinstance(result, TestReport):
        if result.run_id != run_id:
            raise ProviderError("Judge Provider returned a report for another Run")
        return result
    if not isinstance(result, Mapping):
        raise ProviderError("Judge Provider must return TestReport or a mapping")
    if contains_private_data(result):
        raise ProviderError("Judge Provider report contains private fields")
    if "status" not in result:
        raise ProviderError("Judge Provider report is missing status")
    _ensure_json(result, "Judge Provider report")
    supplied_run_id = result.get("run_id", run_id)
    if supplied_run_id != run_id:
        raise ProviderError("Judge Provider returned a report for another Run")
    supplied_extensions = result.get("extensions") or {}
    if not isinstance(supplied_extensions, Mapping):
        raise ProviderError("Report extensions must be a mapping")
    extensions = dict(supplied_extensions)
    extensions.update({str(key): result[key] for key in set(result) - _REPORT_FIELDS})
    try:
        return TestReport(
            schema_version=result.get("schema_version", "defuzex.report.v1"),
            report_id=result.get("report_id") or _new_id("report"),
            run_id=run_id,
            status=result["status"],
            confidence=result.get("confidence"),
            stop_reason=result.get("stop_reason", "case_completed"),
            issues=tuple(result.get("issues") or ()),
            evidence_gaps=tuple(result.get("evidence_gaps") or ()),
            extensions=extensions,
        )
    except (TypeError, ValidationError) as exc:
        raise ProviderError("Judge Provider returned an invalid TestReport") from exc


__all__ = ["normalize_case", "normalize_report"]
