"""Repository metadata, Agent Profile, capability, and privacy boundaries."""

from .agent_profiles import AgentProfileSpec, parse_agent_profile
from .strategy_groups import (
    STRATEGY_GROUP_CATALOG_SCHEMA_VERSION,
    STRATEGY_GROUP_SELECTION_SCHEMA_VERSION,
    StrategyGroupCatalog,
    StrategyGroupDeclaration,
    validate_strategy_group_catalog,
    validate_strategy_group_declaration,
    validate_strategy_group_wire_selection,
)
from .tool_capabilities import (
    AGENT_CAPABILITIES_SCHEMA_VERSION,
    AgentCapabilities,
    ResourceScope,
    ToolCapability,
    scan_agent_tools,
    validate_agent_capabilities,
)
from .tool_capability_io import (
    load_agent_capabilities,
    save_agent_capabilities,
    scan_agent_tool_manifest,
)

__all__ = [
    "AGENT_CAPABILITIES_SCHEMA_VERSION",
    "STRATEGY_GROUP_CATALOG_SCHEMA_VERSION",
    "STRATEGY_GROUP_SELECTION_SCHEMA_VERSION",
    "AgentCapabilities",
    "AgentProfileSpec",
    "ResourceScope",
    "StrategyGroupCatalog",
    "StrategyGroupDeclaration",
    "ToolCapability",
    "load_agent_capabilities",
    "parse_agent_profile",
    "save_agent_capabilities",
    "scan_agent_tool_manifest",
    "scan_agent_tools",
    "validate_agent_capabilities",
    "validate_strategy_group_catalog",
    "validate_strategy_group_declaration",
    "validate_strategy_group_wire_selection",
]
