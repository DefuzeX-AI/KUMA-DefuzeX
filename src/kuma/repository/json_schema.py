"""Offline JSON Schema validation shared by requirements and tool contracts."""

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
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            raise ValidationError(
                "Input schema may only use internal $ref values",
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
        ValidationError: If the schema uses an external ``$ref`` or is invalid
            for its declared JSON Schema dialect.

    Postconditions:
        Success changes no input or process state.

    Side Effects:
        None; validators cannot retrieve files or network resources.
    """
    _reject_external_schema_references(schema)
    validator = validator_for(schema)
    try:
        validator.check_schema(schema)
    except JsonSchemaSchemaError as exc:
        raise ValidationError(
            "Input schema is not a valid JSON Schema", code="schema_invalid"
        ) from exc


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
