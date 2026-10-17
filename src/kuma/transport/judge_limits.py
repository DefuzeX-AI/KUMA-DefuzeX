"""Shared public discovery/error validation for the final Judge message budget."""

from collections.abc import Mapping
from typing import Any

from ..errors import ProviderError


def validate_judge_input_limit(value: Any) -> dict[str, Any]:
    """Detach the closed final-message budget advertised by the Backend.

    Args:
        value: Present discovery field or payload-too-large details; exactly
            max_input_bytes (strict positive int) and measurement (the literal
            final_judge_message_utf8). Absence is handled by each caller.
    Returns:
        A fresh two-field dictionary describing final system plus serialized
        user-message UTF-8 bytes, not uploaded bytes or tokens. Private overhead
        prevents this informational value from guaranteeing that an upload fits.
    Raises:
        ProviderError: Invalid shape/value, with safe invalid_response wording.
    Side Effects:
        None; shared by configuration and error parsing without I/O or mutation.
    """
    if (
        not isinstance(value, Mapping)
        or set(value) != {"max_input_bytes", "measurement"}
        or type(value["max_input_bytes"]) is not int
        or value["max_input_bytes"] <= 0
        or value["measurement"] != "final_judge_message_utf8"
        or type(value["measurement"]) is not str
    ):
        raise ProviderError(
            "The Backend returned an invalid Judge input limit",
            code="invalid_response",
        )
    return {
        "max_input_bytes": value["max_input_bytes"],
        "measurement": value["measurement"],
    }
