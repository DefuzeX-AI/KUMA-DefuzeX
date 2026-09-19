"""Bind optional public Run correlation to the exact committed upload history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..correlation import RUN_CONTEXT_SCHEMA, validate_run_context
from ..errors import ConfigurationError, ValidationError


def upload_run_context(context: Any, config: Any) -> dict[str, Any] | None:
    """Validate opt-in correlation against negotiated support and actual envelopes.

    None preserves old Judge metadata byte shape. Explicit correlation never
    silently downgrades: unsupported servers raise run_context_unsupported before
    multipart/POST. Every history item must have its real Runtime Evidence
    coordinates; no fake Case/step/submission is generated here.
    """
    if context.run_context is None:
        return None
    if RUN_CONTEXT_SCHEMA not in config.supported_run_context_schemas:
        raise ConfigurationError(
            "This service does not support Run correlation; upgrade the service or omit external IDs",
            code="run_context_unsupported",
        )
    value = validate_run_context(context.run_context)
    valid = value["case_id"] == context.case.case_id and len(value["steps"]) == len(
        context.history
    )
    for item, step in zip(context.history, value["steps"], strict=False):
        envelope = item.submission.extensions.get("runtime_evidence")
        if not isinstance(envelope, Mapping):
            raise ConfigurationError(
                "Run correlation requires captured Runtime Evidence for each submitted step",
                code="run_context_unsupported",
            )
        valid &= value["run_id"] == item.submission.run_id == envelope.get("run_id")
        valid &= (
            step["input_id"] == item.submission.input_id == item.test_input.input_id
        )
        valid &= step["execution_status"] == item.submission.status
        valid &= all(
            step[key] == envelope.get(key)
            for key in ("input_id", "step_id", "submission_id")
        )
    if not valid:
        raise ValidationError(
            "Run context does not match committed Evidence", code="run_context_invalid"
        )
    return value
