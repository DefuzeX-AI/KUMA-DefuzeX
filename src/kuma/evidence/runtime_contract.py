"""Canonical Runtime Evidence wire constants, serialization, and validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .._json_values import JsonStructureError, detach_json

RUNTIME_EVIDENCE_SCHEMA_V1 = "defuzex.runtime_evidence.v1"
RUNTIME_EVIDENCE_SCHEMA_V2 = "defuzex.runtime_evidence.v2"
# Preserve the original import as the v1 name used by existing integrations.
RUNTIME_EVIDENCE_SCHEMA = RUNTIME_EVIDENCE_SCHEMA_V1
RUNTIME_EVIDENCE_MEDIA_TYPE = "application/vnd.defuzex.runtime-evidence+json"
RUNTIME_EVIDENCE_MAX_CHARS = 120_000
RUNTIME_AGENT_OUTPUT_MAX_BYTES = 32_768
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
    """Detach mappings and sequences into plain JSON containers for serialization."""
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
    """Return whether text is non-empty and within its contract length."""
    return isinstance(value, str) and 1 <= len(value) <= maximum


def _optional_hash(component: Mapping[str, Any], name: str) -> bool:
    """Accept an omitted digest or require canonical lowercase SHA-256."""
    return name not in component or normalize_sha256(component[name]) == component[name]


def _required_hash(component: Mapping[str, Any], name: str) -> bool:
    """Require a present canonical lowercase SHA-256 field."""
    value = component.get(name)
    return isinstance(value, str) and normalize_sha256(value) == value


def _relative_wire_path(value: Any) -> bool:
    """Reject absolute, parent-traversing, or oversized public paths."""
    if not _text(value, 1024):
        return False
    normalized = value.replace("\\", "/")
    return not (
        normalized.startswith("/")
        or re.match(r"^[a-zA-Z]:/", normalized)
        or ".." in normalized.split("/")
    )


def _non_negative(value: Any) -> bool:
    """Accept non-negative integers while rejecting booleans."""
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_component_fields(
    component: Mapping[str, Any], *, schema_version: str = RUNTIME_EVIDENCE_SCHEMA_V1
) -> bool:
    """Enforce the closed component fields for the selected wire version.

    Runtime Evidence v2 changes only ``agent_response_claim``: a completed claim
    carries ``agent_output`` while v1 remains hash-only. Required conditional
    semantics are checked by :func:`_valid_component_value`.
    """
    kind = component.get("kind")
    if kind not in _KINDS:
        return False
    allowed = _COMMON_FIELDS | _KIND_FIELDS[kind]
    if schema_version == RUNTIME_EVIDENCE_SCHEMA_V2 and kind == "agent_response_claim":
        allowed |= {"agent_output"}
    required = allowed - _KIND_OPTIONAL_FIELDS[kind]
    if kind == "agent_response_claim":
        required -= {"agent_output"}
    return required <= set(component) <= allowed


def runtime_claim_bytes(value: Any) -> bytes:
    """Return bytes used by the frozen Runtime Evidence claim digest.

    Strings retain the historical raw UTF-8 ``surrogatepass`` encoding. Every
    other value uses finite, detached, key-sorted compact JSON with ASCII escapes.
    This function performs no I/O and never embeds a rejected value in an error.
    """

    if isinstance(value, str):
        return value.encode("utf-8", "surrogatepass")
    plain = detach_json(value)
    return json.dumps(
        plain,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def runtime_agent_output_bytes(value: Any) -> bytes:
    """Serialize one non-null finite Agent output using canonical JSON bytes.

    Unlike :func:`runtime_claim_bytes`, strings include their JSON quotes because
    this byte count governs the actual v2 ``agent_output`` value on the wire.
    Invalid, cyclic, unsupported, or null values raise ``JsonStructureError``.
    """

    if value is None:
        raise JsonStructureError("agent output must not be null")
    plain = detach_json(value)
    return json.dumps(
        plain,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def runtime_claim_sha256(value: Any) -> str:
    """Return the frozen lowercase digest for one Agent response claim."""

    return hashlib.sha256(runtime_claim_bytes(value)).hexdigest()


def _valid_claim_component(
    component: Mapping[str, Any], *, schema_version: str
) -> bool:
    """Validate version-dependent Agent claim fields, hash, and output budget."""

    valid_claim = (
        _text(component["claim_id"], 128)
        and component["claim"] in {"completed", "refused", "blocked"}
        and normalize_sha256(component["text_sha256"]) == component["text_sha256"]
    )
    if not valid_claim:
        return False
    if schema_version == RUNTIME_EVIDENCE_SCHEMA_V1:
        return "agent_output" not in component
    if component["claim"] != "completed":
        return "agent_output" not in component
    if "agent_output" not in component:
        return False
    try:
        encoded = runtime_agent_output_bytes(component["agent_output"])
        digest = runtime_claim_sha256(component["agent_output"])
    except (JsonStructureError, TypeError, ValueError):
        return False
    return (
        len(encoded) <= RUNTIME_AGENT_OUTPUT_MAX_BYTES
        and digest == component["text_sha256"]
    )


def _valid_component_value(
    component: Mapping[str, Any], *, schema_version: str
) -> bool:
    """Validate values for exactly one kind in the frozen Core component union.

    Args:
        component: Closed-field mapping that already passed
            :func:`_valid_component_fields`.

    Returns:
        ``True`` only when kind-specific enums, hashes, identifiers, paths,
        counts, sizes, and required values satisfy the canonical v1 contract.

    Preconditions:
        ``kind`` and every required field exist; callers must run the closed-field
        check first because this function indexes required keys directly.

    Postconditions:
        The mapping is not mutated. ``False`` identifies an invalid public
        component before official upload/model use.

    Security/Privacy:
        Hash fields must be canonical SHA-256 and paths must be bounded relative
        wire paths; raw tool arguments/stdout/log/prompt values are not accepted.
    """
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
    return _valid_claim_component(
        component,
        schema_version=schema_version,
    )


def _validate_association(
    value: Mapping[str, Any],
    *,
    run_id: str,
    input_id: str,
    step_id: str,
    submission_id: str,
    schema_version: str,
) -> None:
    """Require exact Run, Input, step, and Submission correlation before upload."""
    actual = (
        value["run_id"],
        value["input_id"],
        value["step_id"],
        value["submission_id"],
    )
    if (
        value["schema_version"] != schema_version
        or actual != (run_id, input_id, step_id, submission_id)
        or not _text(value["run_id"], 128)
        or not _text(value["input_id"], 128)
        or not _text(value["step_id"], 80)
        or not _text(value["submission_id"], 128)
    ):
        raise ValueError("runtime evidence association is invalid")


def _validate_components(
    components: Any, *, schema_version: str = RUNTIME_EVIDENCE_SCHEMA_V1
) -> None:
    """Require versioned closed components in strictly increasing order."""
    if (
        not isinstance(components, Sequence)
        or isinstance(components, str | bytes | bytearray)
        or not 1 <= len(components) <= 100
    ):
        raise ValueError("runtime evidence components are invalid")
    component_ids: set[str] = set()
    previous_sequence = -1
    for component in components:
        if not isinstance(component, Mapping) or not _valid_component_fields(
            component, schema_version=schema_version
        ):
            raise ValueError("runtime evidence component is invalid")
        component_id, sequence = component["component_id"], component["sequence"]
        if (
            not _text(component_id, 128)
            or component_id in component_ids
            or not _non_negative(sequence)
            or sequence <= previous_sequence
            or not _valid_component_value(component, schema_version=schema_version)
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
    schema_version: str = RUNTIME_EVIDENCE_SCHEMA_V1,
) -> None:
    """Fail closed on malformed, duplicate, or mis-associated public evidence.

    ``schema_version`` is the exact version negotiated with the Backend. Omitting
    it preserves v1 validation for existing callers; unknown versions are never
    inferred from untrusted content.
    """

    if schema_version not in {
        RUNTIME_EVIDENCE_SCHEMA_V1,
        RUNTIME_EVIDENCE_SCHEMA_V2,
    }:
        raise ValueError("runtime evidence schema version is unsupported")

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
        schema_version=schema_version,
    )
    _validate_components(value["components"], schema_version=schema_version)
    if len(runtime_evidence_json(value)) > RUNTIME_EVIDENCE_MAX_CHARS:
        raise ValueError("runtime evidence exceeds the character limit")


__all__ = [
    "CASEGEN_EVIDENCE_CAPABILITY_ORDER",
    "CASEGEN_FRAMEWORK_SCHEMA",
    "RUNTIME_AGENT_OUTPUT_MAX_BYTES",
    "RUNTIME_EVIDENCE_MAX_CHARS",
    "RUNTIME_EVIDENCE_MEDIA_TYPE",
    "RUNTIME_EVIDENCE_SCHEMA",
    "RUNTIME_EVIDENCE_SCHEMA_V1",
    "RUNTIME_EVIDENCE_SCHEMA_V2",
    "casegen_framework_is_advertised",
    "derive_casegen_evidence_capabilities",
    "normalize_sha256",
    "runtime_agent_output_bytes",
    "runtime_claim_bytes",
    "runtime_claim_sha256",
    "runtime_evidence_json",
    "validate_runtime_evidence",
]
