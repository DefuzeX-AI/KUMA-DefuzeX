"""Closed public serializer diagnostics shared by HTTP and async error mapping."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ProviderError

# Frozen Backend public serializer constraints, not user values or Core fields.
FIELD_CONSTRAINTS = {
    "strategy_id": {"expected_type": "string", "maximum": 40},
    "strategy_version": {"expected_type": "string", "maximum": 40},
    "count": {"expected_type": "integer", "minimum": 1},
    "max_steps": {"expected_type": "integer", "minimum": 1},
    "repo_meta": {"expected_type": "object"},
    "agent_description": {"expected_type": "string", "maximum": 2000},
    "behavior_spec": {"expected_type": "object"},
    "behavior_spec.production_scenario": {"expected_type": "string", "maximum": 4000},
    "behavior_spec.behaviors_to_test": {"expected_type": "string", "maximum": 4000},
    "behavior_spec.prohibited_behaviors": {"expected_type": "string", "maximum": 4000},
    "tool_list": {"expected_type": "array", "maximum": 100},
    "evidence_capabilities": {"expected_type": "array", "minimum": 1, "maximum": 7},
    "status": {
        "expected_type": "string",
        "allowed_values": ["completed", "failed", "timeout", "aborted"],
    },
    "force": {"expected_type": "boolean"},
    "allow_sensitive": {"expected_type": "boolean"},
    "agent_name": {"expected_type": "string", "maximum": 160},
    "agent_version": {"expected_type": "string", "maximum": 160},
    "repo_fingerprint": {"expected_type": "string", "maximum": 71},
    "case_sha256": {"expected_type": "string"},
    "case_signature": {"expected_type": "string", "maximum": 255},
    "manifest": {"expected_type": "object"},
}
_FIELD_REASONS = {
    "required": "必须提供",
    "invalid_type": "类型不正确",
    "blank": "不能为空",
    "min_value": "低于允许下限",
    "max_value": "超过允许上限",
    "max_length": "长度超过允许上限",
    "invalid_choice": "不在允许值中",
    "invalid": "不符合要求",
}


def validated_field_details(details: object) -> dict[str, Any]:
    """Detach bounded static input diagnostics at the public transport boundary.

    Args:
        details: Optional-error details already present on an invalid_request
            envelope. Requires exactly fields, a list of one to sixteen records.
            Each record requires a frozen public field and reason; optional
            constraints must exactly match that field's static declaration.

    Returns:
        Detached plain dictionaries/lists safe for exception details and display.

    Raises:
        ProviderError: Unknown fields/keys, dynamic bounds/choices, wrong types,
            or malformed records produce invalid_response without raw values.

    Security/Privacy:
        HTTP and async callers share this validator before mapping or clearing
        pending state. No I/O, user values, free-form text or private paths are
        accepted; no truth is inferred from untrusted serializer messages.
    """
    records = details.get("fields") if isinstance(details, Mapping) else None
    if (
        not isinstance(details, Mapping)
        or set(details) != {"fields"}
        or type(records) is not list
        or not 1 <= len(records) <= 16
    ):
        raise ProviderError("Invalid public field diagnostics", code="invalid_response")
    result = []
    for record in records:
        field = record.get("field") if isinstance(record, Mapping) else None
        reason = record.get("reason") if isinstance(record, Mapping) else None
        if (
            type(field) is not str
            or field not in FIELD_CONSTRAINTS
            or type(reason) is not str
            or reason not in _FIELD_REASONS
        ):
            raise ProviderError(
                "Invalid public field diagnostics", code="invalid_response"
            )
        constraints = FIELD_CONSTRAINTS[field]
        projected: dict[str, Any] = {"field": field, "reason": reason}
        for key, value in record.items():
            if key in {"field", "reason"}:
                continue
            expected = constraints.get(key)
            if (
                type(value) is not type(expected)
                or value != expected
                or key not in constraints
            ):
                raise ProviderError(
                    "Invalid public field diagnostics", code="invalid_response"
                )
            if isinstance(value, list) and any(type(item) is not str for item in value):
                raise ProviderError(
                    "Invalid public field diagnostics", code="invalid_response"
                )
            projected[key] = list(value) if isinstance(value, list) else value
        result.append(projected)
    return {"fields": result}


def field_error_message(details: Mapping[str, Any]) -> str:
    """Format only validated public constraints into actionable Chinese text.

    Args:
        details: Detached output of validated_field_details, never raw wire data.

    Returns:
        User-input correction instructions naming each safe field. String bounds
        use characters, array bounds use item counts, and integer bounds use values.

    Security/Privacy:
        Called only by the shared HTTP/async message mapper. Does not interpolate
        remote message or submitted values and does not advise automatic retries.
    """
    messages = []
    types = {
        "integer": "整数",
        "string": "字符串",
        "boolean": "布尔值",
        "array": "数组",
        "object": "对象",
    }
    for record in details["fields"]:
        text = f"{record['field']}：{_FIELD_REASONS[record['reason']]}"  # noqa: RUF001
        kind = record.get("expected_type")
        if kind:
            text += f"（要求{types[kind]}）"  # noqa: RUF001
        unit = {"string": "字符", "array": "项"}.get(
            FIELD_CONSTRAINTS[record["field"]]["expected_type"], ""
        )
        for bound, label in (("minimum", "最小"), ("maximum", "最大")):
            if bound in record:
                text += f"，{label}{record[bound]}{unit}"  # noqa: RUF001
        if "allowed_values" in record:
            text += "，允许值：" + ", ".join(record["allowed_values"])  # noqa: RUF001
        messages.append(text)
    return "提交的内容有问题：" + "；".join(messages) + "。请修改这些字段后重试。"  # noqa: RUF001
