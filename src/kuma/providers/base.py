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
    """Detach and recursively freeze Provider context metadata."""
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class CaseGenerationContext:
    """Describe exactly what a Case Provider may use to create one public Case.

    A custom provider receives this object in ``generate_case``. It may inspect
    the explicitly selected repository and requirement data, then must return a
    public :class:`~kuma.contracts.Case` (or a supported mapping) containing no
    private rubric or hidden answer. The object is immutable so provider code
    cannot change the values used by later normalization and judging.

    Attributes:
        repo_path: Absolute path to the repository selected by the caller. The
            provider may inspect this repository only; it must not infer that
            parent directories or unrelated paths are in scope.
        repo_meta: Read-only repository metadata collected by KUMA, such as the
            bounded tree and repository fingerprint. It is not repository file
            content. Official providers reduce it to the public HTTP allowlist
            before transmission.
        requirement: Requirement body supplied by the caller, or ``None`` when
            no requirement file was selected. Custom providers that declare
            ``requirement_required=False`` may support the omitted case.
        input_type: Required public input payload kind: ``"text"`` or
            ``"structured"``. Returned Case inputs must use this kind.
        input_schema: Read-only JSON Schema for structured inputs, or ``None``
            when no schema applies. A provider must not mutate this mapping.
        strategy: Requested public Case strategy identifier. ``"auto"`` asks
            an official service to select a supported strategy; custom providers
            decide how to interpret it and must not treat it as private rubric.
        max_steps: Positive upper bound on the number of Case steps the provider
            may return. It is a maximum, not a request for exactly that many
            steps; returning any non-empty sequence up to this value is valid.
        agent_description: Front-matter description of the Agent under test, or
            ``None``. It deliberately excludes the requirement body and secrets.
        requirement_sections: Read-only named sections parsed from the public
            requirement. Values remain local unless the selected provider's
            documented public contract transmits an allowlisted subset.

    Security/Privacy:
        This context never contains a private rubric, hidden answer, provider
        credential, MCP address, or model configuration. Possession of
        ``repo_path`` is not permission to read outside that repository.
    """

    repo_path: Path
    repo_meta: Mapping[str, Any]
    requirement: str | None
    input_type: str
    input_schema: Mapping[str, Any] | None
    strategy: str
    max_steps: int
    agent_description: str | None = None
    requirement_sections: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Resolve and freeze provider inputs before the provider is invoked.

        Raises:
            ConfigurationError: If ``max_steps`` is not positive.

        Preconditions:
            The caller has already chosen the repository and parsed any
            requirement file; this method does not read repository contents.

        Postconditions:
            ``repo_path`` is absolute and the three mapping attributes are
            detached, read-only snapshots. A failure leaves no external state.

        Side Effects:
            Resolves the path lexically/filesystem-wise through ``Path.resolve``;
            performs no network request and writes no files.
        """
        path_failed = False
        try:
            resolved_repo = self.repo_path.resolve()
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
            path_failed = True
        if path_failed:
            raise ConfigurationError(
                "Case Provider repository path is unavailable"
            ) from None
        object.__setattr__(self, "repo_path", resolved_repo)
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
        if self.max_steps <= 0:
            raise ConfigurationError(
                "Custom Case Providers require a positive max_steps"
            )


@dataclass(frozen=True, slots=True)
class JudgeContext:
    """Supply one completed public Run to a Judge Provider.

    Attributes:
        case: Public Case whose inputs were executed. Private official rubric
            provenance, when present, remains an opaque validated extension.
        history: Ordered immutable pairs of delivered inputs and committed
            submissions. The order is the order observed by the Run.
        run_status: Public terminal Run status passed to the provider. Current
            callers use ``"completed"`` before judging.
        evidence_summary: Read-only bounded summary such as history length and
            dropped-evidence count. It is not a substitute for per-submission
            Evidence and contains no private Judge material.

    Security/Privacy:
        Custom judges receive only public Case data and SDK-collected Run
        history. Official judges serialize this context through the public
        Backend boundary; they never receive a private rubric from the SDK.
    """

    case: Case
    history: tuple[HistoryItem, ...]
    run_status: str
    evidence_summary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Detach the completed history and summary before Judge invocation.

        Preconditions:
            ``history`` contains only committed :class:`HistoryItem` values from
            one Run; the Run lifecycle is responsible for that correlation.

        Postconditions:
            ``history`` is a tuple and ``evidence_summary`` is a read-only copy.

        Side Effects:
            Performs no file, network, model, or credential access.
        """
        object.__setattr__(self, "history", tuple(self.history))
        object.__setattr__(
            self, "evidence_summary", _immutable_mapping(self.evidence_summary)
        )


@runtime_checkable
class CaseProvider(Protocol):
    """Port for producing one complete Case before a Run delivers any Input."""

    def generate_case(self, context: CaseGenerationContext) -> Any:
        """Produce one public Case for a validated generation context.

        Args:
            context: Immutable repository, requirement, strategy, and step-limit
                inputs described by :class:`CaseGenerationContext`.

        Returns:
            A :class:`Case`; a Case mapping with required ``inputs``; one text
            or :class:`KumaInput`; or a ``list``/``tuple`` of public Inputs.
            A top-level mapping without ``inputs`` is not an Input fallback.

        Raises:
            ProviderError: If generation fails or no valid public Case can be
                returned. Implementations may raise a more specific ``KumaError``.

        Postconditions:
            A successful result contains one through ``context.max_steps``
            inputs and no private rubric, expected output, hidden answer, or
            provider secret.
        """
        ...


@runtime_checkable
class JudgeProvider(Protocol):
    """Port for synchronously judging a completed Run history."""

    def judge(self, context: JudgeContext) -> Any:
        """Judge one completed Run without mutating its committed history.

        Args:
            context: Immutable public Case, ordered history, terminal status,
                and bounded Evidence summary for exactly one Run.

        Returns:
            A :class:`TestReport` or supported mapping that KUMA normalizes into
            the stable public report contract.

        Raises:
            ProviderError: If judging fails or returns an invalid public result.

        Postconditions:
            Success yields a report correlated to the context Run; failure must
            not fabricate a report or rewrite committed submissions.
        """
        ...


@dataclass(frozen=True, slots=True)
class CallableCaseProvider:
    """Expose a plain Python callback through the typed Case Provider protocol.

    Attributes:
        callback: Function called once with :class:`CaseGenerationContext`; its
            return value follows :meth:`CaseProvider.generate_case`.
        requirement_required: Whether :func:`kuma.create_run` must receive a
            requirement before invoking this callback. Set ``False`` only when
            the callback intentionally supports requirement-free generation.
    """

    callback: Callable[[CaseGenerationContext], Any]
    requirement_required: bool = True

    def generate_case(self, context: CaseGenerationContext) -> Any:
        """Invoke the callback while preserving only safe public failures.

        Args:
            context: Immutable inputs for the Case generation request.

        Returns:
            The callback result, which the caller subsequently normalizes.

        Raises:
            ProviderError: Re-raises an intentional provider error, or replaces
                any other callback exception with a stable non-sensitive error.

        Side Effects:
            Whatever the user-supplied callback performs. KUMA itself performs
            no additional I/O in this adapter.

        Security/Privacy:
            Arbitrary callback exception text is not exposed through the SDK.
        """
        try:
            return self.callback(context)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("The custom Case Provider failed") from exc


@dataclass(frozen=True, slots=True)
class CallableJudgeProvider:
    """Expose a plain Python callback through the typed Judge Provider protocol.

    Attributes:
        callback: Function called with one immutable :class:`JudgeContext`; its
            result follows :meth:`JudgeProvider.judge`.
    """

    callback: Callable[[JudgeContext], Any]

    def judge(self, context: JudgeContext) -> Any:
        """Invoke the callback while preserving only safe public failures.

        Args:
            context: Completed Run data supplied to the custom judge.

        Returns:
            The callback result, which KUMA subsequently normalizes.

        Raises:
            ProviderError: Re-raises an intentional provider error, or maps an
                arbitrary callback exception to a safe stable failure.

        Security/Privacy:
            Arbitrary callback exception text is not propagated to callers.
        """
        try:
            return self.callback(context)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("The custom Judge Provider failed") from exc


def adapt_case_provider(
    provider: CaseProvider | Callable[[CaseGenerationContext], Any],
) -> CaseProvider:
    """Return a Case Provider for either supported user-facing input form.

    Args:
        provider: Existing :class:`CaseProvider` instance or a callback accepting
            :class:`CaseGenerationContext`.

    Returns:
        ``provider`` unchanged when it implements the protocol; otherwise a
        :class:`CallableCaseProvider` wrapper.

    Raises:
        ConfigurationError: If the value is neither a protocol implementation
            nor callable.

    Postconditions:
        The returned object always exposes ``generate_case(context)``.
    """
    if isinstance(provider, CaseProvider):
        return provider
    if callable(provider):
        return CallableCaseProvider(provider)
    raise ConfigurationError("case_provider must implement CaseProvider or be callable")


def adapt_judge_provider(
    provider: JudgeProvider | Callable[[JudgeContext], Any],
) -> JudgeProvider:
    """Return a Judge Provider for either supported user-facing input form.

    Args:
        provider: Existing :class:`JudgeProvider` instance or a callback
            accepting :class:`JudgeContext`.

    Returns:
        ``provider`` unchanged when it implements the protocol; otherwise a
        :class:`CallableJudgeProvider` wrapper.

    Raises:
        ConfigurationError: If the value is neither a protocol implementation
            nor callable.

    Postconditions:
        The returned object always exposes ``judge(context)``.
    """
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
