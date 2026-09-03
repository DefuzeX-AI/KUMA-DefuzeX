"""Normalize flexible custom Provider results into stable v4 contracts."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .._json_values import detach_json
from ..contracts import Case, KumaInput, TestReport
from ..errors import ProviderError, ValidationError
from ..repository.json_schema import validate_schema, validate_structured_input
from ..repository.privacy import contains_private_data

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
_PRIVATE_INPUT_FIELDS = ("rubric",)
_PRIVATE_CASE_ERROR = "Custom Case contains prohibited private fields"


def _new_id(prefix: str) -> str:
    """Create a random local identifier for normalized custom Provider data."""
    return f"{prefix}_{uuid.uuid4().hex}"


def _plain_json(value: Any) -> Any:
    """Detach bounded Provider JSON or raise one non-sensitive Provider error."""
    try:
        return detach_json(value)
    except Exception:
        raise ProviderError("Provider value must be JSON serializable") from None


def _ensure_json(value: Any, description: str) -> None:
    """Detach a Provider value and reject non-JSON or non-finite content."""
    try:
        _plain_json(value)
    except ProviderError:
        raise ProviderError(f"{description} must be JSON serializable") from None


def _bounded_inputs(value: Any, max_steps: int) -> list[Any]:
    """Accept only the documented single-input or list/tuple fallback shapes."""
    if isinstance(value, (str, bytes, Mapping, KumaInput)):
        return [value]
    if not isinstance(value, (list, tuple)):
        raise ProviderError(
            "Case Provider must return a Case, explicit Case mapping, single text "
            "or KumaInput, or a list/tuple of Inputs"
        )
    inputs: list[Any] = []
    for item in value:
        inputs.append(item)
        if len(inputs) > max_steps:
            raise ProviderError("Case Provider returned more than max_steps")
    return inputs


def _public_input_value(value: Any) -> Any:
    """Project a typed Input to the same public fields scanned on mapping inputs."""
    if not isinstance(value, KumaInput):
        return value
    return {
        "payload": value.payload,
        "public_constraints": value.public_constraints,
        "extensions": value.extensions,
    }


def _reject_private_case_data(value: Any) -> None:
    """Reject private evaluation keys without retaining or echoing their values."""
    if contains_private_data(value, extra_fields=_PRIVATE_INPUT_FIELDS):
        raise ProviderError(_PRIVATE_CASE_ERROR)


def _case_envelope_json(result: Mapping[Any, Any]) -> tuple[dict[str, Any], Any]:
    """Validate Case metadata as JSON while preserving the typed Input field.

    Args:
        result: Custom Provider Case envelope. Its key that stringifies to
            ``inputs`` may contain documented :class:`KumaInput` instances;
            every other field must be ordinary bounded JSON.

    Returns:
        A detached JSON envelope whose ``inputs`` value is a harmless sentinel,
        plus the original Input value for the dedicated Input normalizer.

    Raises:
        ProviderError: If mapping iteration, key conversion, envelope traversal,
            cycle/depth validation, or the required ``inputs`` field fails.

    Preconditions:
        ``result`` came from a custom Case Provider and has not been exposed as
        a public :class:`Case`.

    Postconditions:
        Success proves all non-Input metadata is bounded acyclic JSON. Input
        contents remain untouched for typed projection and per-Input checks.

    Side Effects:
        Iterates the Provider mapping locally. It performs no persistence,
        Evidence capture, network request, model call, or billing action.

    Security/Privacy:
        Traversal failures become stable Provider errors and never include a
        key, value, object representation, or original exception.
    """
    missing = object()
    raw_inputs: Any = missing
    envelope: dict[str, Any] = {}
    try:
        for key, value in result.items():
            normalized_key = str(key)
            if normalized_key == "inputs":
                raw_inputs = value
                envelope[normalized_key] = None
            else:
                envelope[normalized_key] = value
    except Exception:
        raise ProviderError("Provider value must be JSON serializable") from None

    plain = _plain_json(envelope)
    if raw_inputs is missing:
        _reject_private_case_data(plain)
        raise ProviderError("Case Provider mapping must contain an 'inputs' field")
    return plain, raw_inputs


def _input_parts(
    value: Any,
) -> tuple[Any, str, str | None, Mapping[str, Any], Mapping[str, Any]]:
    """Validate and detach one Input mapping into normalized public parts."""
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
    """Detach the supported public fields from a raw Case Provider result.

    Attributes:
        inputs: Raw input value or sequence awaiting normalization.
        case_id: Optional provider-supplied public Case identifier.
        input_type: Optional declared ``text``/``structured`` payload kind.
        input_schema: Optional public structured-input JSON Schema.
        rubric: Optional public custom-provider rubric; official private rubric
            content is forbidden here.
        extensions: Optional public extension metadata awaiting validation.
    """

    inputs: Any
    case_id: str | None = None
    input_type: str | None = None
    input_schema: Mapping[str, Any] | None = None
    rubric: Mapping[str, Any] | None = None
    extensions: Mapping[str, Any] | None = None


def _case_parts(result: Any) -> _CaseParts:
    """Detach one supported Case shape without serializing typed Inputs.

    Mapping envelopes are split at the public ``inputs`` boundary. Non-Input
    metadata is validated as one bounded JSON graph, while typed Inputs retain
    their public type until the normalizer projects and scans their payload,
    constraints, and extensions. This preserves the documented Case mapping
    contract without allowing opaque objects in metadata.
    """
    if isinstance(result, Case):
        _reject_private_case_data(
            {
                "inputs": [_public_input_value(item) for item in result.inputs],
                "input_schema": result.input_schema,
                "extensions": result.extensions,
            }
        )
        _reject_private_case_data(result.rubric)
        return _CaseParts(
            inputs=result.inputs,
            case_id=result.case_id,
            input_type=result.input_type,
            input_schema=result.input_schema,
            rubric=result.rubric,
            extensions=result.extensions,
        )
    if not isinstance(result, Mapping):
        return _CaseParts(inputs=result)
    result, raw_inputs = _case_envelope_json(result)

    public_case = {key: value for key, value in result.items() if key != "rubric"}
    _reject_private_case_data(public_case)
    _reject_private_case_data(result.get("rubric"))
    supplied_extensions = result.get("extensions", {})
    if not isinstance(supplied_extensions, Mapping):
        raise ProviderError("Case extensions must be a mapping")
    extensions = dict(supplied_extensions)
    extensions.update({str(key): result[key] for key in set(result) - _CASE_FIELDS})
    return _CaseParts(
        inputs=raw_inputs,
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
    """Resolve Case input type while enforcing the requirement's declared type."""
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
    """Resolve and validate the schema required for structured Inputs."""
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
    """Validate every structured Input against the accepted JSON Schema."""
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
    """Create immutable Inputs with unique IDs and validated payload shapes."""
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
    max_steps: int,
    required_input_type: str | None,
    required_input_schema: Mapping[str, Any] | None,
) -> Case:
    """Validate and freeze one Provider Case before any Input is exposed.

    ``create_run`` calls this boundary for both official normalized mappings and
    custom Provider output. It enforces the Run's Input limit, requirement type
    and schema, unique identities, JSON safety, and reserved-extension rules,
    returning the immutable public ``Case`` contract or raising ``ProviderError``.

    Args:
        result: Provider result as a :class:`Case`, Case mapping with required
            ``inputs``, one text/:class:`KumaInput`, or a ``list``/``tuple`` of
            public Inputs. Other mappings and iterables are rejected.
        run_id: Owning Run identifier assigned to every normalized input.
        max_steps: Positive maximum number of inputs accepted; fewer are valid.
        required_input_type: Required ``text``/``structured`` type from the
            requirement, or ``None`` when provider declaration decides.
        required_input_schema: Required structured JSON Schema, or ``None``.

    Returns:
        New immutable :class:`Case` with correlated IDs and detached JSON values.

    Raises:
        ProviderError: If shape, count, identifiers, schema, payload, rubric, or
            extensions violate the public Provider contract.

    Preconditions:
        ``run_id`` identifies the Run being assembled and ``max_steps`` is the
        selected provider boundary.

    Postconditions:
        Success contains one through ``max_steps`` inputs, all sharing Run/Case
        identity and payload type. Caller-owned objects cannot mutate it.

    Security/Privacy:
        Private fields and forged official provenance from custom providers fail
        closed; normalization never manufactures official status.
    """

    if max_steps <= 0:
        raise ProviderError("max_steps must be positive")
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

    raw_sequence = _bounded_inputs(parts.inputs, max_steps)
    if not raw_sequence:
        raise ProviderError("Case Provider returned no Inputs")
    parsed = []
    for value in raw_sequence:
        public_value = _public_input_value(value)
        scanned_value = (
            public_value if isinstance(value, KumaInput) else _plain_json(public_value)
        )
        _reject_private_case_data(scanned_value)
        parsed.append(
            _input_parts(value if isinstance(value, KumaInput) else public_value)
        )
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
    """Normalize custom Judge output into the stable public Report contract."""
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
