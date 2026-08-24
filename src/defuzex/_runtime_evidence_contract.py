"""Canonical Runtime Evidence wire constants, serialization, and validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

RUNTIME_EVIDENCE_SCHEMA = "defuzex.runtime_evidence.v1"
RUNTIME_EVIDENCE_MEDIA_TYPE = "application/vnd.defuzex.runtime-evidence+json"
RUNTIME_EVIDENCE_MAX_CHARS = 120_000
CASEGEN_FRAMEWORK_SCHEMA = "defuzex.casegen.ita.v1"
CASEGEN_EVIDENCE_CAPABILITY_ORDER = (
    "file_change",
    "tool_call",
    "command_result",
    "test_result",
    "state_transition",
    "artifact_snapshot",
    "agent_response_claim",
)
_HASH = re.compile(r"[0-9a-f]{64}")
_KINDS = frozenset(
    {
        "file_change",
        "tool_call",
        "command_result",
        "test_result",
        "state_transition",
        "artifact_snapshot",
        "agent_response_claim",
    }
)
_COMMON_FIELDS = frozenset({"component_id", "sequence", "kind"})
_KIND_FIELDS = {
    "file_change": frozenset(
        {"path", "change_type", "before_sha256", "after_sha256", "size_bytes"}
    ),
    "tool_call": frozenset(
        {"tool_name", "outcome", "arguments_sha256", "result_sha256"}
    ),
    "command_result": frozenset(
        {"command_id", "exit_code", "stdout_sha256", "stderr_sha256"}
    ),
    "test_result": frozenset({"suite_id", "outcome", "passed", "failed", "skipped"}),
    "state_transition": frozenset(
        {"state_id", "outcome", "before_sha256", "after_sha256"}
    ),
    "artifact_snapshot": frozenset(
        {"artifact_id", "path", "sha256", "size_bytes", "media_type"}
    ),
    "agent_response_claim": frozenset({"claim_id", "claim", "text_sha256"}),
}
_KIND_OPTIONAL_FIELDS = {
    "file_change": frozenset({"before_sha256", "after_sha256", "size_bytes"}),
    "tool_call": frozenset({"result_sha256"}),
    "command_result": frozenset({"stdout_sha256", "stderr_sha256"}),
    "test_result": frozenset(),
    "state_transition": frozenset({"before_sha256", "after_sha256"}),
    "artifact_snapshot": frozenset({"path"}),
    "agent_response_claim": frozenset(),
}


def runtime_evidence_json(value: Mapping[str, Any]) -> str:
    """Serialize one canonical envelope as bounded UTF-8 JSON text."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(child) for child in value]
    return value


def normalize_sha256(value: Any) -> str | None:
    """Return one lowercase 64-hex digest, accepting the SDK's local prefix."""

    if not isinstance(value, str):
        return None
    candidate = value.removeprefix("sha256:").casefold()
    return candidate if _HASH.fullmatch(candidate) else None


def derive_casegen_evidence_capabilities(
    *, track_files: bool, trace_evidence_configured: bool
) -> tuple[str, ...]:
    """Declare only canonical component kinds this Run configuration can emit."""

    declared = set()
    if track_files:
        declared.add("file_change")
    if trace_evidence_configured:
        declared.add("artifact_snapshot")
    if declared:
        declared.add("agent_response_claim")
    return tuple(item for item in CASEGEN_EVIDENCE_CAPABILITY_ORDER if item in declared)


def casegen_framework_is_advertised(entitlements: Mapping[str, Any]) -> bool:
    """Return whether entitlements explicitly advertise the IT+A public bridge."""

    protocol = entitlements.get("protocol")
    if not isinstance(protocol, Mapping):
        return False
    frameworks = protocol.get("casegen_frameworks")
    return (
        isinstance(frameworks, list)
        and all(isinstance(item, str) for item in frameworks)
        and CASEGEN_FRAMEWORK_SCHEMA in frameworks
    )


def _text(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= maximum


def _optional_hash(component: Mapping[str, Any], name: str) -> bool:
    return name not in component or normalize_sha256(component[name]) == component[name]


def _required_hash(component: Mapping[str, Any], name: str) -> bool:
    value = component.get(name)
    return isinstance(value, str) and normalize_sha256(value) == value


def _relative_wire_path(value: Any) -> bool:
    if not _text(value, 1024):
        return False
    normalized = value.replace("\\", "/")
    return not (
        normalized.startswith("/")
        or re.match(r"^[a-zA-Z]:/", normalized)
        or ".." in normalized.split("/")
    )


def _non_negative(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_component_fields(component: Mapping[str, Any]) -> bool:
    kind = component.get("kind")
    if kind not in _KINDS:
        return False
    allowed = _COMMON_FIELDS | _KIND_FIELDS[kind]
    required = allowed - _KIND_OPTIONAL_FIELDS[kind]
    return required <= set(component) <= allowed


def _valid_component_value(component: Mapping[str, Any]) -> bool:
    kind = component["kind"]
    if kind == "file_change":
        return (
            _relative_wire_path(component["path"])
            and component["change_type"]
            in {"created", "modified", "deleted", "unchanged"}
            and _optional_hash(component, "before_sha256")
            and _optional_hash(component, "after_sha256")
            and (
                "size_bytes" not in component or _non_negative(component["size_bytes"])
            )
        )
    if kind == "tool_call":
        return (
            _text(component["tool_name"], 128)
            and component["outcome"] in {"succeeded", "failed", "unknown"}
            and _required_hash(component, "arguments_sha256")
            and _optional_hash(component, "result_sha256")
        )
    if kind == "command_result":
        return (
            _text(component["command_id"], 128)
            and isinstance(component["exit_code"], int)
            and not isinstance(component["exit_code"], bool)
            and _optional_hash(component, "stdout_sha256")
            and _optional_hash(component, "stderr_sha256")
        )
    if kind == "test_result":
        return (
            _text(component["suite_id"], 128)
            and component["outcome"] in {"passed", "failed", "partial"}
            and all(
                _non_negative(component[name])
                for name in ("passed", "failed", "skipped")
            )
        )
    if kind == "state_transition":
        return (
            _text(component["state_id"], 128)
            and component["outcome"] in {"succeeded", "failed", "unknown"}
            and _optional_hash(component, "before_sha256")
            and _optional_hash(component, "after_sha256")
        )
    if kind == "artifact_snapshot":
        return (
            _text(component["artifact_id"], 128)
            and ("path" not in component or _relative_wire_path(component["path"]))
            and normalize_sha256(component["sha256"]) == component["sha256"]
            and _non_negative(component["size_bytes"])
            and _text(component["media_type"], 128)
        )
    return (
        _text(component["claim_id"], 128)
        and component["claim"] in {"completed", "refused", "blocked"}
        and normalize_sha256(component["text_sha256"]) == component["text_sha256"]
    )


def _validate_association(
    value: Mapping[str, Any],
    *,
    run_id: str,
    input_id: str,
    step_id: str,
    submission_id: str,
) -> None:
    actual = (
        value["run_id"],
        value["input_id"],
        value["step_id"],
        value["submission_id"],
    )
    if (
        value["schema_version"] != RUNTIME_EVIDENCE_SCHEMA
        or actual != (run_id, input_id, step_id, submission_id)
        or not _text(value["run_id"], 128)
        or not _text(value["input_id"], 128)
        or not _text(value["step_id"], 80)
        or not _text(value["submission_id"], 128)
    ):
        raise ValueError("runtime evidence association is invalid")


def _validate_components(components: Any) -> None:
    if (
        not isinstance(components, Sequence)
        or isinstance(components, str | bytes | bytearray)
        or not 1 <= len(components) <= 100
    ):
        raise ValueError("runtime evidence components are invalid")
    component_ids: set[str] = set()
    previous_sequence = -1
    for component in components:
        if not isinstance(component, Mapping) or not _valid_component_fields(component):
            raise ValueError("runtime evidence component is invalid")
        component_id, sequence = component["component_id"], component["sequence"]
        if (
            not _text(component_id, 128)
            or component_id in component_ids
            or not _non_negative(sequence)
            or sequence <= previous_sequence
            or not _valid_component_value(component)
        ):
            raise ValueError("runtime evidence component is invalid")
        component_ids.add(component_id)
        previous_sequence = sequence


def validate_runtime_evidence(
    value: Any,
    *,
    run_id: str,
    input_id: str,
    step_id: str,
    submission_id: str,
) -> None:
    """Fail closed on malformed, duplicate, or mis-associated public evidence."""

    fields = {
        "schema_version",
        "run_id",
        "input_id",
        "step_id",
        "submission_id",
        "components",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("runtime evidence envelope is invalid")
    _validate_association(
        value,
        run_id=run_id,
        input_id=input_id,
        step_id=step_id,
        submission_id=submission_id,
    )
    _validate_components(value["components"])
    if len(runtime_evidence_json(value)) > RUNTIME_EVIDENCE_MAX_CHARS:
        raise ValueError("runtime evidence exceeds the character limit")


__all__ = [
    "CASEGEN_EVIDENCE_CAPABILITY_ORDER",
    "CASEGEN_FRAMEWORK_SCHEMA",
    "RUNTIME_EVIDENCE_MAX_CHARS",
    "RUNTIME_EVIDENCE_MEDIA_TYPE",
    "RUNTIME_EVIDENCE_SCHEMA",
    "casegen_framework_is_advertised",
    "derive_casegen_evidence_capabilities",
    "normalize_sha256",
    "runtime_evidence_json",
    "validate_runtime_evidence",
]
