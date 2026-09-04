# Agent Profile terminology migration

KUMA now calls the user-authored context document an **Agent Profile**. This is
a one-time breaking API migration; there are no aliases, fallback keywords, or
deprecated modules.

| Removed name | Current name |
| --- | --- |
| `requirement_path` | `agent_profile_path` |
| `RequirementSpec` | `AgentProfileSpec` |
| `parse_requirement` | `parse_agent_profile` |
| `kuma.repository.requirements` | `kuma.repository.agent_profiles` |
| `CaseGenerationContext.requirement` | `CaseGenerationContext.agent_profile` |
| `requirement_sections` | `agent_profile_sections` |
| `requirement_required` | `agent_profile_required` |
| `requirement_invalid` | `agent_profile_invalid` |
| `requirement.md` | `agent-profile.md` |

Update imports, keyword arguments, custom `CaseProvider` declarations, error-code
handling, and filenames together. Calls using a removed name fail instead of
silently selecting compatibility behavior.

This terminology also clarifies product ownership: Strategy Group selects the
testing capability, domain, and method. Agent Profile describes the Agent,
production scenario, expected behavior, and prohibited boundaries for that
selection. Its prose cannot infer or override an explicit Group; without an
explicit declaration KUMA still uses the catalog default unless local scanning
was explicitly enabled. The existing Backend/Core wire fields
`agent_description` and `behavior_spec` are unchanged.
