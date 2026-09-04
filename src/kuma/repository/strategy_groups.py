"""Closed Strategy Group catalog, selection, and local matching contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..errors import ValidationError
from ..evidence.runtime_contract import CASEGEN_EVIDENCE_CAPABILITY_ORDER
from .tool_capabilities import AgentCapabilities

STRATEGY_GROUP_SELECTION_SCHEMA_VERSION = "kuma.strategy_group_selection.v1"
STRATEGY_GROUP_CATALOG_SCHEMA_VERSION = "kuma.strategy_group_catalog.v1"
SelectionSource = Literal["user", "scanner", "general"]

_CATALOG_RELEASE = re.compile(r"[0-9a-f]{64}")
_DIFFICULTIES = ("D0", "D1", "D2")
_DECLARATION_FIELDS = frozenset({"schema_version", "id", "version"})
_WIRE_SELECTION_FIELDS = frozenset(
    {
        "schema_version",
        "strategy_group_id",
        "strategy_group_version",
        "selection_source",
        "catalog_release",
    }
)
_CATALOG_FIELDS = frozenset(
    {"schema_version", "catalog_release", "default", "limits", "groups"}
)
_GROUP_FIELDS = frozenset(
    {
        "id",
        "version",
        "display_name",
        "description",
        "required_capabilities",
        "available",
        "limits",
    }
)
_LEGACY_CATALOG_FIELDS = frozenset({"default_strategy_id", "strategies"})
_LEGACY_STRATEGY_FIELDS = frozenset(
    {
        "strategy_id",
        "description",
        "default_version",
        "supported_versions",
        "max_count",
    }
)


@dataclass(frozen=True, slots=True)
class StrategyGroupDeclaration:
    """Represent a user-selected Strategy Group coordinate from an Agent Profile.

    Attributes:
        id: Stable catalog group identifier.
        version: Exact compiled group version requested by the user.
    """

    id: str
    version: str

    def to_dict(self) -> dict[str, str]:
        """Return the closed Agent Profile front-matter JSON projection."""
        return {
            "schema_version": STRATEGY_GROUP_SELECTION_SCHEMA_VERSION,
            "id": self.id,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class StrategyGroupLimits:
    """Hold public execution bounds attached to one catalog group.

    Attributes:
        max_steps: Maximum number of Case steps supported by this group.
        supported_difficulties: Canonically ordered supported D0-D2 overlays.
    """

    max_steps: int
    supported_difficulties: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the detached closed public limits object."""
        return {
            "max_steps": self.max_steps,
            "supported_difficulties": list(self.supported_difficulties),
        }


@dataclass(frozen=True, slots=True)
class StrategyGroup:
    """Hold one validated public Strategy Group catalog entry.

    Attributes:
        id: Stable group identifier.
        version: Exact compiled version.
        display_name: Human-readable catalog label.
        description: Public explanation of the behavior family.
        required_capabilities: Canonically ordered Runtime Evidence capabilities
            that the Run must be able to produce before this group can be used.
        available: Whether the current service permits new selections.
        limits: Group-specific public step and difficulty limits.
    """

    id: str
    version: str
    display_name: str
    description: str
    required_capabilities: tuple[str, ...]
    available: bool
    limits: StrategyGroupLimits

    @property
    def coordinate(self) -> tuple[str, str]:
        """Return the immutable ``(id, version)`` lookup coordinate."""
        return self.id, self.version

    def to_dict(self) -> dict[str, Any]:
        """Return the detached closed public catalog entry."""
        return {
            "id": self.id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "required_capabilities": list(self.required_capabilities),
            "available": self.available,
            "limits": self.limits.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class StrategyGroupCatalog:
    """Represent one strictly validated Backend Strategy Group catalog.

    Attributes:
        catalog_release: Immutable lowercase SHA-256 release identity.
        default: Exact coordinate for semantic General fallback.
        groups: Unique entries ordered by ID and version.
    """

    catalog_release: str
    default: StrategyGroupDeclaration
    groups: tuple[StrategyGroup, ...]

    def group(self, coordinate: StrategyGroupDeclaration) -> StrategyGroup | None:
        """Return the group at ``coordinate``, or ``None`` when absent."""
        return next(
            (
                group
                for group in self.groups
                if group.coordinate == (coordinate.id, coordinate.version)
            ),
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical public catalog JSON object for CLI output."""
        return {
            "schema_version": STRATEGY_GROUP_CATALOG_SCHEMA_VERSION,
            "catalog_release": self.catalog_release,
            "default": {"id": self.default.id, "version": self.default.version},
            "limits": {"max_selected_groups": 1},
            "groups": [group.to_dict() for group in self.groups],
        }


@dataclass(frozen=True, slots=True)
class ResolvedStrategyGroup:
    """Bind a catalog coordinate to its safe selection provenance.

    Attributes:
        group: Exact validated selected catalog entry.
        selection_source: ``user``, conservative ``scanner``, or ``general``.
        catalog_release: Release against which the selection was resolved.
    """

    group: StrategyGroup
    selection_source: SelectionSource
    catalog_release: str

    def to_wire(self) -> dict[str, str]:
        """Return the exact outbound Case request selection object."""
        return {
            "schema_version": STRATEGY_GROUP_SELECTION_SCHEMA_VERSION,
            "strategy_group_id": self.group.id,
            "strategy_group_version": self.group.version,
            "selection_source": self.selection_source,
            "catalog_release": self.catalog_release,
        }

    def to_declaration(self) -> dict[str, str]:
        """Return an editable Agent Profile selection declaration."""
        return StrategyGroupDeclaration(self.group.id, self.group.version).to_dict()


def _invalid(message: str, *, code: str = "strategy_group_invalid") -> ValidationError:
    """Create one stable local Strategy Group validation error."""
    return ValidationError(message, code=code)


def _closed(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    """Require a plain mapping with the exact fields owned by one schema node."""
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _invalid(f"{label} has invalid fields")
    return value


def _text(value: Any, *, label: str, maximum: int) -> str:
    """Validate bounded printable catalog text without echoing caller content."""
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise _invalid(
            f"{label} must be a non-empty string of at most {maximum} characters"
        )
    if any(not character.isprintable() for character in value):
        raise _invalid(f"{label} contains non-printable characters")
    return value.strip()


def validate_strategy_group_declaration(value: Any) -> StrategyGroupDeclaration:
    """Validate the closed Agent Profile ``strategy_group`` declaration.

    The user may provide only an exact group ID and version. Catalog release and
    selection provenance are service/runtime facts and are therefore rejected in
    this local declaration.
    """
    raw = _closed(value, _DECLARATION_FIELDS, "strategy_group")
    if raw["schema_version"] != STRATEGY_GROUP_SELECTION_SCHEMA_VERSION:
        raise _invalid("Unsupported strategy_group schema_version")
    return StrategyGroupDeclaration(
        id=_text(raw["id"], label="strategy_group id", maximum=80),
        version=_text(raw["version"], label="strategy_group version", maximum=32),
    )


def validate_strategy_group_wire_selection(value: Any) -> dict[str, str]:
    """Validate and detach the exact resolved Case-request selection object."""
    raw = _closed(value, _WIRE_SELECTION_FIELDS, "strategy_group_selection")
    if raw["schema_version"] != STRATEGY_GROUP_SELECTION_SCHEMA_VERSION:
        raise _invalid("Unsupported strategy_group_selection schema_version")
    source = raw["selection_source"]
    release = raw["catalog_release"]
    if type(source) is not str or source not in {"user", "scanner", "general"}:
        raise _invalid("strategy_group_selection source is invalid")
    if type(release) is not str or _CATALOG_RELEASE.fullmatch(release) is None:
        raise _invalid("strategy_group_selection catalog_release is invalid")
    return {
        "schema_version": STRATEGY_GROUP_SELECTION_SCHEMA_VERSION,
        "strategy_group_id": _text(
            raw["strategy_group_id"], label="strategy group id", maximum=80
        ),
        "strategy_group_version": _text(
            raw["strategy_group_version"],
            label="strategy group version",
            maximum=32,
        ),
        "selection_source": source,
        "catalog_release": release,
    }


def _ordered_capabilities(value: Any) -> tuple[str, ...]:
    """Require a unique capability list already in canonical Evidence order."""
    if type(value) is not list or any(type(item) is not str for item in value):
        raise _invalid("required_capabilities must be an array of strings")
    selected = set(value)
    ordered = tuple(
        item for item in CASEGEN_EVIDENCE_CAPABILITY_ORDER if item in selected
    )
    if len(value) != len(selected) or tuple(value) != ordered:
        raise _invalid("required_capabilities must be unique and canonically ordered")
    return ordered


def _group_limits(value: Any) -> StrategyGroupLimits:
    """Validate one group's closed step and difficulty limits."""
    raw = _closed(
        value, frozenset({"max_steps", "supported_difficulties"}), "group limits"
    )
    max_steps = raw["max_steps"]
    difficulties = raw["supported_difficulties"]
    if (
        isinstance(max_steps, bool)
        or not isinstance(max_steps, int)
        or not 1 <= max_steps <= 10
    ):
        raise _invalid("group max_steps must be an integer from 1 through 10")
    if type(difficulties) is not list or any(
        type(item) is not str for item in difficulties
    ):
        raise _invalid("supported_difficulties must be an array of strings")
    ordered = tuple(item for item in _DIFFICULTIES if item in set(difficulties))
    if len(difficulties) != len(set(difficulties)) or tuple(difficulties) != ordered:
        raise _invalid("supported_difficulties must be unique and canonically ordered")
    return StrategyGroupLimits(max_steps=max_steps, supported_difficulties=ordered)


def _group(value: Any) -> StrategyGroup:
    """Validate and detach one closed catalog group entry."""
    raw = _closed(value, _GROUP_FIELDS, "strategy group")
    if type(raw["available"]) is not bool:
        raise _invalid("strategy group available must be a boolean")
    return StrategyGroup(
        id=_text(raw["id"], label="strategy group id", maximum=80),
        version=_text(raw["version"], label="strategy group version", maximum=32),
        display_name=_text(
            raw["display_name"], label="strategy group display_name", maximum=120
        ),
        description=_text(
            raw["description"], label="strategy group description", maximum=500
        ),
        required_capabilities=_ordered_capabilities(raw["required_capabilities"]),
        available=raw["available"],
        limits=_group_limits(raw["limits"]),
    )


def validate_strategy_group_catalog(value: Any) -> StrategyGroupCatalog:
    """Validate the exact v1 catalog returned by ``GET /sdk/strategies/``.

    Raises:
        ValidationError: With ``strategy_group_invalid`` for unknown fields,
            invalid limits/order, duplicate coordinates, or an unsafe default.
    """
    raw = _closed(value, _CATALOG_FIELDS, "strategy group catalog")
    if raw["schema_version"] != STRATEGY_GROUP_CATALOG_SCHEMA_VERSION:
        raise _invalid("Unsupported strategy group catalog schema_version")
    release = raw["catalog_release"]
    if type(release) is not str or _CATALOG_RELEASE.fullmatch(release) is None:
        raise _invalid(
            "strategy group catalog_release must be 64 lowercase hex characters"
        )
    limits = _closed(
        raw["limits"], frozenset({"max_selected_groups"}), "catalog limits"
    )
    if (
        limits["max_selected_groups"] != 1
        or type(limits["max_selected_groups"]) is not int
    ):
        raise _invalid("catalog max_selected_groups must equal 1")
    default_raw = _closed(
        raw["default"], frozenset({"id", "version"}), "catalog default"
    )
    default = StrategyGroupDeclaration(
        id=_text(default_raw["id"], label="catalog default id", maximum=80),
        version=_text(
            default_raw["version"], label="catalog default version", maximum=32
        ),
    )
    values = raw["groups"]
    if type(values) is not list or not 1 <= len(values) <= 128:
        raise _invalid("catalog groups must contain between 1 and 128 entries")
    groups = tuple(_group(item) for item in values)
    coordinates = tuple(group.coordinate for group in groups)
    if coordinates != tuple(sorted(coordinates)) or len(set(coordinates)) != len(
        coordinates
    ):
        raise _invalid(
            "catalog groups must have unique canonically ordered coordinates"
        )
    catalog = StrategyGroupCatalog(release, default, groups)
    fallback = catalog.group(default)
    if fallback is None or not fallback.available or fallback.required_capabilities:
        raise _invalid(
            "catalog default must identify an available capability-free group"
        )
    return catalog


def is_legacy_strategy_catalog(value: Any) -> bool:
    """Recognize only the exact bounded catalog emitted by legacy Backends.

    This predicate exists solely to decide whether an undeclared Strategy Group
    may use the old Case wire. It therefore validates the complete public legacy
    shape before granting compatibility; malformed, extra, or private-looking
    fields fall through to the strict current-catalog validator and fail closed.
    """
    if not isinstance(value, Mapping) or set(value) != _LEGACY_CATALOG_FIELDS:
        return False
    default_id = value.get("default_strategy_id")
    strategies = value.get("strategies")
    if not _legacy_identifier(default_id, maximum=40):
        return False
    if type(strategies) is not list or not 1 <= len(strategies) <= 128:
        return False
    coordinates: list[str] = []
    for strategy in strategies:
        if not _legacy_strategy(strategy):
            return False
        coordinates.append(strategy["strategy_id"])
    return (
        coordinates == sorted(coordinates)
        and len(set(coordinates)) == len(coordinates)
        and default_id in set(coordinates)
    )


def _legacy_identifier(value: Any, *, maximum: int) -> bool:
    """Check one legacy ID/version without normalizing untrusted wire text."""
    return (
        type(value) is str
        and 1 <= len(value) <= maximum
        and value == value.strip()
        and all(character.isprintable() for character in value)
    )


def _legacy_strategy(value: Any) -> bool:
    """Validate one deployed legacy catalog entry and its relational bounds."""
    if not isinstance(value, Mapping) or set(value) != _LEGACY_STRATEGY_FIELDS:
        return False
    versions = value.get("supported_versions")
    default_version = value.get("default_version")
    description = value.get("description")
    max_count = value.get("max_count")
    if not _legacy_identifier(value.get("strategy_id"), maximum=40):
        return False
    if not _legacy_identifier(default_version, maximum=40):
        return False
    if type(description) is not str or len(description) > 500:
        return False
    if type(versions) is not list or not 1 <= len(versions) <= 128:
        return False
    if not all(_legacy_identifier(version, maximum=40) for version in versions):
        return False
    if versions != sorted(versions) or len(set(versions)) != len(versions):
        return False
    if default_version not in versions:
        return False
    return type(max_count) is int and 1 <= max_count <= 32_767


def available_evidence_capabilities(
    document: AgentCapabilities | None,
    intrinsic: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the canonical union of declared tool and intrinsic Run Evidence.

    Tool declarations remain user-controlled claims: validation proves syntax
    and safety, not that a tool exists or emits the stated Evidence.
    """
    values = set(intrinsic)
    if document is not None:
        for tool in document.tools:
            values.update(tool.evidence_types)
    return tuple(item for item in CASEGEN_EVIDENCE_CAPABILITY_ORDER if item in values)


def resolve_strategy_group(
    catalog: StrategyGroupCatalog,
    *,
    explicit: StrategyGroupDeclaration | None,
    scan: bool,
    available_capabilities: tuple[str, ...],
) -> ResolvedStrategyGroup:
    """Resolve explicit, conservative scanner, or catalog-default selection.

    Explicit user choice has priority. Scanner mode considers only available
    non-default groups whose required capabilities are a subset of the derived
    capability set, keeps those with maximum required-capability cardinality, and uses
    one only when that maximum is unique. It never guesses from tool names,
    schemas, descriptions, resources, access, or side effects.
    """
    available = set(available_capabilities)
    if explicit is not None:
        group = catalog.group(explicit)
        if group is None or not group.available:
            raise _invalid("The declared strategy group is unavailable")
        source: SelectionSource = "user"
    elif scan:
        candidates = [
            group
            for group in catalog.groups
            if group.coordinate != (catalog.default.id, catalog.default.version)
            and group.available
            and set(group.required_capabilities).issubset(available)
        ]
        maximum = max(
            (len(group.required_capabilities) for group in candidates), default=-1
        )
        best = [
            group for group in candidates if len(group.required_capabilities) == maximum
        ]
        if len(best) == 1:
            group = best[0]
            source = "scanner"
        else:
            group = catalog.group(catalog.default)
            source = "general"
    else:
        group = catalog.group(catalog.default)
        source = "general"
    if group is None:
        raise _invalid("The strategy group catalog default is missing")
    missing = tuple(
        item for item in group.required_capabilities if item not in available
    )
    if missing:
        raise ValidationError(
            "The selected strategy group requires unavailable Runtime Evidence capabilities",
            code="strategy_capability_mismatch",
            details={"missing_capabilities": list(missing)},
        )
    return ResolvedStrategyGroup(group, source, catalog.catalog_release)


def load_strategy_group_catalog(path: str | Path) -> StrategyGroupCatalog:
    """Read and validate one explicit local catalog for offline suggestion."""
    try:
        payload = Path(path).read_text(encoding="utf-8")
        if len(payload.encode("utf-8")) > 262_144:
            raise _invalid(
                "Strategy group catalog exceeds the local size limit",
                code="strategy_scan_invalid",
            )
        return validate_strategy_group_catalog(json.loads(payload))
    except ValidationError:
        raise _invalid(
            "Strategy group catalog is invalid", code="strategy_scan_invalid"
        ) from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _invalid(
            "Strategy group catalog is unreadable or invalid",
            code="strategy_scan_invalid",
        ) from None


__all__ = [
    "STRATEGY_GROUP_CATALOG_SCHEMA_VERSION",
    "STRATEGY_GROUP_SELECTION_SCHEMA_VERSION",
    "ResolvedStrategyGroup",
    "StrategyGroup",
    "StrategyGroupCatalog",
    "StrategyGroupDeclaration",
    "available_evidence_capabilities",
    "is_legacy_strategy_catalog",
    "load_strategy_group_catalog",
    "resolve_strategy_group",
    "validate_strategy_group_catalog",
    "validate_strategy_group_declaration",
    "validate_strategy_group_wire_selection",
]
