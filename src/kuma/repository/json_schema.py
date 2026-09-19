"""Offline JSON Schema validation shared by Agent Profiles and tool contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import SchemaError as JsonSchemaSchemaError
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for

from ..errors import ValidationError


def _reject_external_schema_references(value: Any) -> None:
    """Reject non-local JSON Schema references without retrieving resources."""
    if isinstance(value, Mapping):
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            if keyword not in value:
                continue
            reference = value[keyword]
            if not isinstance(reference, str) or not reference.startswith("#"):
                raise ValidationError(
                    "Input schema may only use internal references",
                    code="schema_invalid",
                )
        for child in value.values():
            _reject_external_schema_references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_external_schema_references(child)


def validate_schema(schema: Mapping[str, Any]) -> None:
    """Validate JSON Schema syntax locally without external reference retrieval.

    Args:
        schema: JSON-compatible schema mapping selected by the caller.

    Raises:
        ValidationError: If the schema uses an external ``$ref``, ``$dynamicRef``
            or ``$recursiveRef``, or its dialect/contents are invalid. Dialect
            selection and schema-library failures become safe ``schema_invalid``;
            tool-document callers map that to ``tool_capabilities_invalid``.

    Postconditions:
        Success changes no input or process state. Rejection retains no library
        exception chain or rejected schema values.

    Side Effects:
        None; validators cannot retrieve files or network resources.
    """
    _reject_external_schema_references(schema)
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise ValidationError(
            "Input schema dialect must be a string", code="schema_invalid"
        )
    valid = False
    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        valid = True
    except (JsonSchemaSchemaError, TypeError, ValueError, AttributeError):
        pass
    if not valid:
        raise ValidationError(
            "Input schema is not a valid JSON Schema", code="schema_invalid"
        )


def validate_structured_input(payload: Any, schema: Mapping[str, Any]) -> None:
    """Validate one generated structured Input against its accepted local schema.

    Args:
        payload: JSON-compatible value returned as the Case input.
        schema: Previously validated local JSON Schema.

    Raises:
        ValidationError: If ``payload`` does not satisfy ``schema``.

    Postconditions:
        Success changes neither payload nor schema.

    Side Effects:
        None.
    """
    validator = validator_for(schema)
    try:
        validator(schema).validate(payload)
    except JsonSchemaValidationError as exc:
        raise ValidationError(
            "Structured input does not satisfy the accepted JSON Schema",
            code="schema_invalid",
        ) from exc


__all__ = ["validate_schema", "validate_structured_input"]
