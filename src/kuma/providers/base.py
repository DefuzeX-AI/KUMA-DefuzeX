"""Provider ports shared by official and custom v4 execution paths."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ..contracts import Case, HistoryItem
from ..errors import ConfigurationError, ProviderError


def _immutable_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class CaseGenerationContext:
    """Validated local inputs made available to a Case Provider.

    ``repo_meta`` is metadata only. Official Providers reduce it again to the
    public wire allowlist before any network request.
    """

    repo_path: Path
    repo_meta: Mapping[str, Any]
    requirement: str | None
    input_type: str
    input_schema: Mapping[str, Any] | None
    strategy: str
    max_inputs: int
    agent_description: str | None = None
    requirement_sections: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_path", self.repo_path.resolve())
        object.__setattr__(self, "repo_meta", _immutable_mapping(self.repo_meta))
        if self.input_schema is not None:
            object.__setattr__(
                self, "input_schema", _immutable_mapping(self.input_schema)
            )
        object.__setattr__(
            self,
            "requirement_sections",
            MappingProxyType(dict(self.requirement_sections)),
        )
        if self.max_inputs <= 0:
            raise ConfigurationError(
                "Custom Case Providers require a positive max_inputs"
            )


@dataclass(frozen=True, slots=True)
class JudgeContext:
    """Immutable completed Run history supplied to a Judge Provider."""

    case: Case
    history: tuple[HistoryItem, ...]
    run_status: str
    evidence_summary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "history", tuple(self.history))
        object.__setattr__(
            self, "evidence_summary", _immutable_mapping(self.evidence_summary)
        )


@runtime_checkable
class CaseProvider(Protocol):
    """Port for producing one complete Case before a Run delivers any Input."""

    def generate_case(self, context: CaseGenerationContext) -> Any: ...


@runtime_checkable
class JudgeProvider(Protocol):
    """Port for synchronously judging a completed Run history."""

    def judge(self, context: JudgeContext) -> Any: ...


@dataclass(frozen=True, slots=True)
class CallableCaseProvider:
    """Adapt a Case callback while replacing unsafe callback error text."""

    callback: Callable[[CaseGenerationContext], Any]
    requirement_required: bool = True

    def generate_case(self, context: CaseGenerationContext) -> Any:
        try:
            return self.callback(context)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("The custom Case Provider failed") from exc


@dataclass(frozen=True, slots=True)
class CallableJudgeProvider:
    """Adapt a Judge callback while replacing unsafe callback error text."""

    callback: Callable[[JudgeContext], Any]

    def judge(self, context: JudgeContext) -> Any:
        try:
            return self.callback(context)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("The custom Judge Provider failed") from exc


def adapt_case_provider(
    provider: CaseProvider | Callable[[CaseGenerationContext], Any],
) -> CaseProvider:
    if isinstance(provider, CaseProvider):
        return provider
    if callable(provider):
        return CallableCaseProvider(provider)
    raise ConfigurationError("case_provider must implement CaseProvider or be callable")


def adapt_judge_provider(
    provider: JudgeProvider | Callable[[JudgeContext], Any],
) -> JudgeProvider:
    if isinstance(provider, JudgeProvider):
        return provider
    if callable(provider):
        return CallableJudgeProvider(provider)
    raise ConfigurationError(
        "judge_provider must implement JudgeProvider or be callable"
    )


__all__ = [
    "CallableCaseProvider",
    "CallableJudgeProvider",
    "CaseGenerationContext",
    "CaseProvider",
    "JudgeContext",
    "JudgeProvider",
    "adapt_case_provider",
    "adapt_judge_provider",
]
