"""Local, framework-neutral Agent tool capability contracts and scanner."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..errors import ValidationError
from .json_schema import validate_schema
from .privacy import enforce_sensitive_policy, scan_sensitive_json

AGENT_CAPABILITIES_SCHEMA_VERSION = "kuma.agent_tool_capabilities.v1"
MAX_CAPABILITY_FILE_BYTES = 262_144
MAX_TOOLS = 100
MAX_SCHEMA_DEPTH = 32

_PROVENANCE = frozenset({"scanner_generated", "user_declared"})
_SIDE_EFFECTS = frozenset(
    {
        "external_state_change",
        "filesystem_write",
        "network_access",
        "process_execution",
    }
)
_RESOURCE_KINDS = frozenset(
    {
        "external_service",
        "network",
        "process",
        "repository",
        "temporary_directory",
        "workspace",
    }
)
_RESOURCE_ACCESS = frozenset({"connect", "execute", "read", "write"})
_EVIDENCE_TYPES = frozenset(
    {
        "agent_response_claim",
        "artifact_snapshot",
        "command_result",
        "file_change",
        "state_transition",
        "test_result",
        "tool_call",
    }
)
_DOCUMENT_FIELDS = frozenset({"schema_version", "provenance", "tools"})
_TOOL_FIELDS = frozenset(
    {
        "evidence_types",
        "input_schema",
        "name",
        "read_only",
        "resource_scopes",
        "side_effects",
        "version",
    }
)
_RESOURCE_FIELDS = frozenset({"access", "resource"})


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """Describe one low-sensitivity resource boundary declared by a tool.

    Attributes:
        resource: Closed resource category such as ``repository`` or ``network``;
            raw paths, hostnames, credentials, and configuration values are not
            accepted.
        access: Operation permitted within that category: ``read``, ``write``,
            ``execute``, or ``connect``.
    """

    resource: str
    access: str

    def to_dict(self) -> dict[str, str]:
        """Return the canonical JSON object for this resource declaration."""
        return {"access": self.access, "resource": self.resource}


@dataclass(frozen=True, slots=True)
class ToolCapability:
    """Hold one validated framework-neutral tool capability declaration.

    Attributes:
        name: Stable public tool name exposed by the Agent.
        version: Tool contract version, or ``None`` when the Agent does not
            publish one.
        input_schema: Detached JSON Schema describing accepted tool arguments.
        read_only: User/scanner declaration that the tool does not mutate
            external state. KUMA records but cannot prove this claim.
        side_effects: Sorted closed categories the tool may cause.
        resource_scopes: Sorted low-sensitivity resource categories and access.
        evidence_types: Sorted canonical Runtime Evidence component kinds the
            integration can actually emit for this tool.
    """

    name: str
    version: str | None
    input_schema: Mapping[str, Any]
    read_only: bool
    side_effects: tuple[str, ...]
    resource_scopes: tuple[ResourceScope, ...]
    evidence_types: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a detached canonical JSON object without executable values."""
        return {
            "evidence_types": list(self.evidence_types),
            "input_schema": _plain_json(self.input_schema),
            "name": self.name,
            "read_only": self.read_only,
            "resource_scopes": [scope.to_dict() for scope in self.resource_scopes],
            "side_effects": list(self.side_effects),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    """Represent one local versioned Agent tool capability document.

    Attributes:
        schema_version: Exact local contract version. Version 1 is
            ``kuma.agent_tool_capabilities.v1``.
        provenance: ``scanner_generated`` when KUMA normalized an explicitly
            supplied tool manifest, or ``user_declared`` for a manually authored
            canonical file. Both remain user-controlled declarations.
        tools: Deterministically ordered validated tool declarations.

    Security/Privacy:
        This document contains only schema and low-sensitivity categories. It is
        never uploaded by the current Official Case wire.
    """

    schema_version: str
    provenance: str
    tools: tuple[ToolCapability, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON document for review, editing, or saving."""
        return {
            "provenance": self.provenance,
            "schema_version": self.schema_version,
            "tools": [tool.to_dict() for tool in self.tools],
        }


def _plain_json(value: Any, *, depth: int = 0) -> Any:
    """Detach plain JSON values while rejecting custom objects and excessive depth."""
    if depth > MAX_SCHEMA_DEPTH:
        raise ValidationError(
            "Tool capability JSON is too deeply nested",
            code="tool_capabilities_invalid",
        )
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValidationError(
                "Tool capability JSON contains a non-finite number",
                code="tool_capabilities_invalid",
            )
        return value
    if type(value) is list:
        return [_plain_json(child, depth=depth + 1) for child in value]
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            raise ValidationError(
                "Tool capability JSON object keys must be strings",
                code="tool_capabilities_invalid",
            )
        return {
            key: _plain_json(child, depth=depth + 1) for key, child in value.items()
        }
    if isinstance(value, MappingProxyType):
        return _plain_json(dict(value), depth=depth)
    raise ValidationError(
        "Tool capabilities must contain plain JSON values only",
        code="tool_capabilities_invalid",
    )


def _closed_mapping(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    """Require a JSON object with exactly the fields owned by one schema level."""
    plain = _plain_json(value)
    if type(plain) is not dict:
        raise ValidationError(
            f"{label} must be a JSON object", code="tool_capabilities_invalid"
        )
    actual = set(plain)
    if actual != fields:
        raise ValidationError(
            f"{label} fields are invalid", code="tool_capabilities_invalid"
        )
    return plain


def _bounded_string(value: Any, *, label: str, maximum: int) -> str:
    """Validate one non-empty bounded string without exposing its value in errors."""
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise ValidationError(
            f"{label} must be a non-empty string of at most {maximum} characters",
            code="tool_capabilities_invalid",
        )
    if any(not character.isprintable() for character in value):
        raise ValidationError(
            f"{label} contains non-printable characters",
            code="tool_capabilities_invalid",
        )
    return value.strip()


def _closed_string_list(
    value: Any, *, label: str, allowed: frozenset[str]
) -> tuple[str, ...]:
    """Validate and deterministically sort one unique closed-enum string list."""
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ValidationError(
            f"{label} must be a JSON array of strings",
            code="tool_capabilities_invalid",
        )
    if len(value) != len(set(value)) or not set(value).issubset(allowed):
        raise ValidationError(
            f"{label} contains duplicate or unsupported values",
            code="tool_capabilities_invalid",
        )
    return tuple(sorted(value))


def _resource_scopes(value: Any) -> tuple[ResourceScope, ...]:
    """Validate low-sensitivity resource categories without accepting raw locations."""
    if type(value) is not list or len(value) > 20:
        raise ValidationError(
            "resource_scopes must be an array with at most 20 items",
            code="tool_capabilities_invalid",
        )
    scopes: list[ResourceScope] = []
    for item in value:
        raw = _closed_mapping(item, _RESOURCE_FIELDS, "resource scope")
        resource = raw["resource"]
        access = raw["access"]
        if resource not in _RESOURCE_KINDS or access not in _RESOURCE_ACCESS:
            raise ValidationError(
                "resource scope contains an unsupported category",
                code="tool_capabilities_invalid",
            )
        scopes.append(ResourceScope(resource=resource, access=access))
    if len({(scope.resource, scope.access) for scope in scopes}) != len(scopes):
        raise ValidationError(
            "resource_scopes contains duplicates",
            code="tool_capabilities_invalid",
        )
    return tuple(sorted(scopes, key=lambda scope: (scope.resource, scope.access)))


def _tool(value: Any) -> ToolCapability:
    """Validate one closed tool declaration and preserve its JSON Schema."""
    raw = _closed_mapping(value, _TOOL_FIELDS, "tool capability")
    name = _bounded_string(raw["name"], label="tool name", maximum=128)
    version_value = raw["version"]
    version = (
        None
        if version_value is None
        else _bounded_string(version_value, label="tool version", maximum=64)
    )
    if type(raw["read_only"]) is not bool:
        raise ValidationError(
            "read_only must be a boolean", code="tool_capabilities_invalid"
        )
    schema = raw["input_schema"]
    if type(schema) is not dict:
        raise ValidationError(
            "input_schema must be a JSON object", code="tool_capabilities_invalid"
        )
    validate_schema(schema)
    side_effects = _closed_string_list(
        raw["side_effects"], label="side_effects", allowed=_SIDE_EFFECTS
    )
    if raw["read_only"] and {
        "external_state_change",
        "filesystem_write",
    }.intersection(side_effects):
        raise ValidationError(
            "read_only tools cannot declare state-changing side effects",
            code="tool_capabilities_invalid",
        )
    return ToolCapability(
        name=name,
        version=version,
        input_schema=MappingProxyType(schema),
        read_only=raw["read_only"],
        side_effects=side_effects,
        resource_scopes=_resource_scopes(raw["resource_scopes"]),
        evidence_types=_closed_string_list(
            raw["evidence_types"],
            label="evidence_types",
            allowed=_EVIDENCE_TYPES,
        ),
    )


def validate_agent_capabilities(value: Any) -> AgentCapabilities:
    """Validate a manual or scanner-generated local capability document.

    Args:
        value: Plain JSON object with exact ``schema_version``, ``provenance``,
            and ``tools`` fields. Tool entries use the same closed schema in both
            modes.

    Returns:
        Immutable, deterministically ordered :class:`AgentCapabilities`.

    Raises:
        ValidationError: If schema version, shape, values, limits, JSON Schema,
            duplicate identity, or read-only consistency is invalid.
        SensitiveDataError: If credential/private-data signatures are present.

    Preconditions:
        The caller treats manual declarations as authoritative; this function
        validates syntax and safety but does not verify that tools exist.

    Postconditions:
        Success returns a detached document with no executable objects and a
        canonical tool order. No file, network, or Run state changes.

    Side Effects:
        None.

    Security/Privacy:
        Sensitive values are rejected without being copied into error messages.
    """
    raw = _closed_mapping(value, _DOCUMENT_FIELDS, "capability document")
    if raw["schema_version"] != AGENT_CAPABILITIES_SCHEMA_VERSION:
        raise ValidationError(
            "Unsupported tool capability schema_version",
            code="tool_capabilities_invalid",
        )
    if raw["provenance"] not in _PROVENANCE:
        raise ValidationError(
            "Unsupported tool capability provenance",
            code="tool_capabilities_invalid",
        )
    if type(raw["tools"]) is not list or not 1 <= len(raw["tools"]) <= MAX_TOOLS:
        raise ValidationError(
            f"tools must contain between 1 and {MAX_TOOLS} entries",
            code="tool_capabilities_invalid",
        )
    tools = tuple(
        sorted(
            (_tool(item) for item in raw["tools"]),
            key=lambda item: (item.name, item.version or ""),
        )
    )
    identities = {(tool.name, tool.version) for tool in tools}
    if len(identities) != len(tools):
        raise ValidationError(
            "Tool name/version identities must be unique",
            code="tool_capabilities_invalid",
        )
    document = AgentCapabilities(
        schema_version=AGENT_CAPABILITIES_SCHEMA_VERSION,
        provenance=raw["provenance"],
        tools=tools,
    )
    serialized = _canonical_bytes(document)
    if len(serialized) > MAX_CAPABILITY_FILE_BYTES:
        raise ValidationError(
            "Tool capability document exceeds the size limit",
            code="tool_capabilities_invalid",
        )
    enforce_sensitive_policy(
        scan_sensitive_json(document.to_dict(), location="tool_capabilities"),
        allow_sensitive=False,
    )
    return document


def scan_agent_tools(tools: Sequence[Mapping[str, Any]]) -> AgentCapabilities:
    """Normalize explicitly exposed plain tool descriptors without executing them.

    Args:
        tools: One to 100 plain mapping descriptors using the canonical tool
            fields. Callers obtain these descriptors from their Agent; KUMA does
            not import the Agent, discover modules, or access tool callables.

    Returns:
        Reviewable immutable document with provenance ``scanner_generated``.

    Raises:
        ValidationError: If ``tools`` is not a plain bounded sequence or any
            descriptor violates the canonical schema.
        SensitiveDataError: If a descriptor contains credential/private data.

    Preconditions:
        The caller has explicitly selected and exposed these metadata mappings.

    Postconditions:
        No tool is invoked and no file is written. The result can be converted
        with :meth:`AgentCapabilities.to_dict`, edited, revalidated, then saved.

    Side Effects:
        None.

    Security/Privacy:
        Custom objects/callables are rejected; only bounded plain JSON metadata
        reaches the result. No network operation is available in this module.
    """
    if type(tools) not in {list, tuple}:
        raise ValidationError(
            "Scanner tools must be a list or tuple of plain mappings",
            code="tool_capabilities_invalid",
        )
    return validate_agent_capabilities(
        {
            "provenance": "scanner_generated",
            "schema_version": AGENT_CAPABILITIES_SCHEMA_VERSION,
            "tools": list(tools),
        }
    )


def _canonical_bytes(document: AgentCapabilities) -> bytes:
    """Serialize one validated document deterministically as UTF-8 JSON."""
    return (
        json.dumps(
            document.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


__all__ = [
    "AGENT_CAPABILITIES_SCHEMA_VERSION",
    "AgentCapabilities",
    "ResourceScope",
    "ToolCapability",
    "scan_agent_tools",
    "validate_agent_capabilities",
]
