"""Local parsing and validation for explicit KUMA requirement files."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import yaml

from ..errors import ValidationError
from .json_schema import validate_schema, validate_structured_input

_FRONT_MATTER_BOUNDARY = "---"
if TYPE_CHECKING:
    from .strategy_groups import StrategyGroupDeclaration
    from .tool_capabilities import AgentCapabilities


_ALLOWED_FRONT_MATTER = frozenset(
    {
        "agent_description",
        "input_type",
        "input_schema",
        "strategy_group",
        "tool_capabilities",
    }
)
_SECTION_ALIASES = {
    "production_scenario": ("生产使用场景", "Production Use Scenario"),
    "behaviors_to_test": ("希望测试的行为", "Behaviors to Test"),
    "prohibited_behaviors": (
        "已知限制或禁止行为",
        "Known Limitations or Prohibited Behaviors",
    ),
}
_SCHEMA_SECTION_ALIASES = ("输入 Schema", "Input Schema")
_HEADING_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_FENCE_PATTERN = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RequirementSpec:
    """Hold a parsed public requirement for Case Provider context creation.

    Attributes:
        path: Absolute path of the explicitly selected UTF-8 requirement file.
        content: Complete validated public requirement text.
        agent_description: Pure front-matter Agent description, kept separate
            from the requirement body for official auto-strategy selection.
        input_type: Required Case input kind, ``text`` or ``structured``.
        body: Requirement body after front matter removal.
        sections: Read-only named Markdown sections parsed from ``body``.
        input_schema: Read-only validated JSON Schema for structured inputs.
        input_schema_path: Absolute path of an explicitly referenced schema file,
            or ``None`` when schema is inline/absent.
        tool_capabilities: Validated local Agent tool capability document, or
            ``None`` when the requirement does not link one. It is not uploaded
            by the current Official Case wire.
        tool_capabilities_path: Absolute path of the linked capability file, or
            ``None`` when absent.
        strategy_group: Exact user-selected Strategy Group coordinate, or
            ``None`` to let Run configuration choose scanner/default behavior.

    Security/Privacy:
        Parsing does not make content safe to upload automatically. Official
        providers apply their own public allowlist and sensitive scan.
    """

    path: Path
    content: str
    agent_description: str
    input_type: str
    body: str
    sections: Mapping[str, str]
    input_schema: Mapping[str, Any] | None = None
    input_schema_path: Path | None = None
    tool_capabilities: AgentCapabilities | None = None
    tool_capabilities_path: Path | None = None
    strategy_group: StrategyGroupDeclaration | None = None

    def __post_init__(self) -> None:
        """Freeze parsed requirement metadata while retaining its explicit source path."""
        object.__setattr__(self, "path", self.path.resolve())
        object.__setattr__(self, "sections", MappingProxyType(dict(self.sections)))
        if self.input_schema is not None:
            object.__setattr__(self, "input_schema", _freeze_mapping(self.input_schema))
        if self.input_schema_path is not None:
            object.__setattr__(
                self, "input_schema_path", self.input_schema_path.resolve()
            )
        if self.tool_capabilities_path is not None:
            object.__setattr__(
                self,
                "tool_capabilities_path",
                self.tool_capabilities_path.resolve(),
            )


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Recursively freeze parsed requirement mappings and sequences."""

    def freeze(item: Any) -> Any:
        """Return an immutable copy of the parsed requirement metadata."""
        if isinstance(item, Mapping):
            return MappingProxyType(
                {str(key): freeze(child) for key, child in item.items()}
            )
        if isinstance(item, (list, tuple)):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(value)


def _split_front_matter(content: str) -> tuple[str, str]:
    """Separate optional YAML front matter from the Markdown requirement body."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_BOUNDARY:
        raise ValidationError(
            "Requirement file must start with YAML front matter",
            code="requirement_invalid",
        )
    try:
        closing_index = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == _FRONT_MATTER_BOUNDARY
        )
    except StopIteration as exc:
        raise ValidationError(
            "Requirement YAML front matter is not closed",
            code="requirement_invalid",
        ) from exc
    return "\n".join(lines[1:closing_index]), "\n".join(
        lines[closing_index + 1 :]
    ).strip()


def _parse_front_matter(source: str) -> Mapping[str, Any]:
    """Parse bounded YAML metadata and require a string-keyed mapping."""
    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ValidationError(
            "Requirement YAML front matter is invalid",
            code="requirement_invalid",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ValidationError(
            "Requirement front matter must be a mapping",
            code="requirement_invalid",
        )
    unknown = {str(key) for key in parsed} - _ALLOWED_FRONT_MATTER
    if unknown:
        raise ValidationError(
            f"Unknown requirement front matter fields: {', '.join(sorted(unknown))}",
            code="requirement_invalid",
        )
    return parsed


def _extract_sections(body: str) -> dict[str, str]:
    """Extract uniquely named level-two Markdown sections as immutable text."""
    matches = list(_HEADING_PATTERN.finditer(body))
    sections_by_heading: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections_by_heading[match.group(1).strip()] = body[match.end() : end].strip()

    sections: dict[str, str] = {}
    for canonical, aliases in _SECTION_ALIASES.items():
        matching_heading = next(
            (alias for alias in aliases if alias in sections_by_heading), None
        )
        if matching_heading is None:
            raise ValidationError(
                f"Requirement section is missing: {aliases[0]}",
                code="requirement_invalid",
            )
        section = sections_by_heading[matching_heading]
        if not section:
            raise ValidationError(
                f"Requirement section is empty: {matching_heading}",
                code="requirement_invalid",
            )
        sections[canonical] = section
    schema_heading = next(
        (alias for alias in _SCHEMA_SECTION_ALIASES if alias in sections_by_heading),
        None,
    )
    if schema_heading is not None:
        sections["input_schema"] = sections_by_heading[schema_heading]
    return sections


def _load_schema_from_section(section: str) -> Mapping[str, Any]:
    """Parse an inline fenced JSON Schema without external reference retrieval."""
    match = _FENCE_PATTERN.search(section)
    if match is None:
        raise ValidationError(
            "Input Schema section must contain a JSON code block",
            code="schema_invalid",
        )
    try:
        schema = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "Inline input schema is invalid JSON", code="schema_invalid"
        ) from exc
    if not isinstance(schema, Mapping):
        raise ValidationError(
            "Input schema must be a JSON object", code="schema_invalid"
        )
    return schema


def _load_schema_file(path: Path) -> Mapping[str, Any]:
    """Read a bounded local JSON Schema file selected by the requirement."""
    if not path.is_file():
        raise ValidationError(
            f"Input schema file does not exist: {path}",
            code="schema_invalid",
        )
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "Input schema file is unreadable or invalid JSON", code="schema_invalid"
        ) from exc
    if not isinstance(schema, Mapping):
        raise ValidationError(
            "Input schema must be a JSON object", code="schema_invalid"
        )
    return schema


def _resolve_tool_capabilities_path(requirement_path: Path, declared_path: Any) -> Path:
    """Resolve one requirement-owned capability file without directory escape.

    Args:
        requirement_path: Already resolved requirement file that owns the link.
        declared_path: Relative path string from YAML front matter.

    Returns:
        Absolute path contained by the requirement file's directory.

    Raises:
        ValidationError: If the declaration is empty, absolute, or resolves
            outside the requirement directory through ``..`` or a symlink.

    Security/Privacy:
        Restricting the implicit read prevents a requirement from selecting an
        unrelated credential/configuration file elsewhere on the host.
    """
    if not isinstance(declared_path, str) or not declared_path.strip():
        raise ValidationError(
            "tool_capabilities must be a relative file path",
            code="tool_capabilities_invalid",
        )
    relative = Path(declared_path)
    if relative.is_absolute():
        raise ValidationError(
            "tool_capabilities must be a relative file path",
            code="tool_capabilities_invalid",
        )
    root = requirement_path.parent.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            "tool_capabilities must stay inside the requirement directory",
            code="tool_capabilities_invalid",
        ) from exc
    return resolved


def _load_declared_tool_capabilities(
    requirement_path: Path, declared_path: Any
) -> tuple[AgentCapabilities | None, Path | None]:
    """Load the optional local capability link through its dedicated boundary.

    Args:
        requirement_path: Resolved requirement file that owns the reference.
        declared_path: Front-matter value, or ``None`` when no file is linked.

    Returns:
        ``(document, absolute_path)`` when declared; otherwise ``(None, None)``.

    Raises:
        ValidationError: If the path or capability document is invalid.
        SensitiveDataError: If the selected file path/content is sensitive.

    Side Effects:
        Reads only the explicitly linked file when present.
    """
    if declared_path is None:
        return None, None
    from .tool_capability_io import load_agent_capabilities

    path = _resolve_tool_capabilities_path(requirement_path, declared_path)
    return load_agent_capabilities(path), path


def _declared_strategy_group(value: Any) -> StrategyGroupDeclaration:
    """Validate a present Strategy Group object without service lookup."""
    from .strategy_groups import validate_strategy_group_declaration

    return validate_strategy_group_declaration(value)


def parse_requirement(path: str | Path) -> RequirementSpec:
    """Parse one explicitly selected requirement and its local Input schema.

    Run construction invokes this offline boundary before Case Provider I/O. It
    validates front matter, required behavior sections, and text/structured Input
    declarations; schema files must be local and external references are rejected.
    Read or validation failures raise stable ``ValidationError`` values.

    Args:
        path: Explicit UTF-8 Markdown requirement file selected by the caller.
            A leading UTF-8 byte-order mark is accepted and removed before
            front-matter parsing; other encodings remain invalid.

    Returns:
        Immutable :class:`RequirementSpec` containing front matter, body,
        sections, and a validated optional structured-input schema.

    Raises:
        ValidationError: If file existence/encoding, front matter, Agent
        description, input type, schema path, tool capability link, or JSON
        Schema is invalid.

    Preconditions:
        The caller authorizes reading this file and any relative schema path it
        explicitly declares.

    Postconditions:
        Success returns detached immutable mappings and absolute source paths;
        no Run or process configuration state changes.

    Side Effects:
        Reads the requirement and, when declared, one local schema file and one
        local tool capability file only. Decoding uses ``utf-8-sig`` so an
        optional leading UTF-8 BOM is treated as transport metadata rather than
        requirement content.

    Security/Privacy:
        Parsing does not transmit content. Official-provider allowlisting and
        sensitive scanning remain mandatory before network use.
    """

    requirement_path = Path(path).expanduser().resolve()
    if not requirement_path.is_file():
        raise ValidationError(
            f"Requirement file does not exist: {requirement_path}",
            code="requirement_required",
        )
    try:
        content = requirement_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValidationError(
            "Requirement file must be readable UTF-8 text",
            code="requirement_invalid",
        ) from exc

    front_matter_source, body = _split_front_matter(content)
    front_matter = _parse_front_matter(front_matter_source)
    description = front_matter.get("agent_description")
    if not isinstance(description, str) or not description.strip():
        raise ValidationError(
            "agent_description must be a non-empty string",
            code="requirement_invalid",
        )
    input_type = front_matter.get("input_type")
    if input_type not in {"text", "structured"}:
        raise ValidationError(
            "input_type must be 'text' or 'structured'",
            code="requirement_invalid",
        )
    sections = _extract_sections(body)

    schema: Mapping[str, Any] | None = None
    schema_path: Path | None = None
    declared_schema = front_matter.get("input_schema")
    if input_type == "text" and declared_schema is not None:
        raise ValidationError(
            "input_schema is only valid for structured input",
            code="requirement_invalid",
        )
    if input_type == "structured":
        if declared_schema is not None:
            if not isinstance(declared_schema, str) or not declared_schema.strip():
                raise ValidationError(
                    "input_schema must be a file path", code="schema_invalid"
                )
            schema_path = (requirement_path.parent / declared_schema).resolve()
            schema = _load_schema_file(schema_path)
        elif "input_schema" in sections:
            schema = _load_schema_from_section(sections["input_schema"])
        else:
            raise ValidationError(
                "Structured requirements must declare an input schema",
                code="schema_invalid",
            )
        validate_schema(schema)

    tool_capabilities, tool_capabilities_path = _load_declared_tool_capabilities(
        requirement_path, front_matter.get("tool_capabilities")
    )
    strategy_group = (
        _declared_strategy_group(front_matter["strategy_group"])
        if "strategy_group" in front_matter
        else None
    )

    return RequirementSpec(
        path=requirement_path,
        content=content,
        agent_description=description.strip(),
        input_type=input_type,
        body=body,
        sections=sections,
        input_schema=schema,
        input_schema_path=schema_path,
        tool_capabilities=tool_capabilities,
        tool_capabilities_path=tool_capabilities_path,
        strategy_group=strategy_group,
    )


__all__ = [
    "RequirementSpec",
    "parse_requirement",
    "validate_schema",
    "validate_structured_input",
]
