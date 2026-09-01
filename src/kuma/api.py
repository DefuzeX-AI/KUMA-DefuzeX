"""Top-level v4 API entry points."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_CASE_MAX_STEPS,
    CreateRunConfig,
    resolve_create_run_config,
    write_api_key,
)
from .errors import ConfigurationError, KumaError, ProviderError, ValidationError
from .evidence.runtime_contract import (
    casegen_framework_is_advertised,
    derive_casegen_evidence_capabilities,
)
from .evidence.trace import TraceEvidenceCapture
from .evidence.tracking.evidence import EvidenceCollector
from .providers import (
    OfficialCaseProvider,
    OfficialJudgeProvider,
    adapt_case_provider,
    adapt_judge_provider,
)
from .providers._official_wire import validate_official_case_provenance
from .providers.base import CaseGenerationContext, CaseProvider, JudgeProvider
from .providers.normalization import normalize_case
from .repository.metadata import collect_repo_meta
from .repository.requirements import RequirementSpec, parse_requirement
from .run import Run
from .runtime import RuntimeSession
from .transport.backend import DEFAULT_BASE_URL, BackendClient


def configure(*, api_key: str) -> Path:
    """Validate and atomically persist an API key in the user credential store.

    Args:
        api_key: Printable ASCII KUMA credential beginning with ``dfx_``. The
            encoded value must be at most 512 bytes and contain no whitespace or
            control characters.

    Returns:
        Absolute path of the atomically written ``credentials.json`` file.

    Raises:
        ConfigurationError: If the key format or credential location is invalid,
            or credential storage cannot be written atomically.

    Preconditions:
        ``api_key`` is the complete credential to persist, not a redacted display
        value. ``KUMA_CONFIG_HOME``, when set, points to the directory in which
        the caller intends KUMA to store credentials.

    Postconditions:
        Success leaves one complete JSON credential file at the returned path.
        A failed atomic replacement removes its temporary file instead of
        reporting a partial credential as configured.

    Side Effects:
        Creates the credential directory when needed and atomically replaces the
        KUMA user credential file. No network request is made.

    Security/Privacy:
        The file is restricted to the current user where supported. It contains
        the real key and must not be printed, uploaded, or committed.
    """

    return write_api_key(api_key)


def _resolve_repo_path(repo_path: str | os.PathLike[str]) -> Path:
    """Resolve an authorized repository root and contain local path failures.

    Args:
        repo_path: String or path-like repository directory selected by the caller.

    Returns:
        Canonical absolute directory with symlink and filesystem-root inputs
        rejected.

    Raises:
        ConfigurationError: If the value is not path-like, the path cannot be
            inspected, or it violates the repository-root safety constraints.

    Security/Privacy:
        Operating-system errors are replaced with a fixed message so exception
        text cannot expose a host path, errno, or platform-specific diagnostic.
    """

    try:
        expanded = Path(repo_path).expanduser()
        if expanded.is_symlink():
            raise ConfigurationError("repo_path must not be a symbolic link")
        resolved = expanded.resolve()
        if not resolved.is_dir():
            raise ConfigurationError("repo_path must be an existing directory")
        if resolved == Path(resolved.anchor):
            raise ConfigurationError("repo_path must not be a filesystem root")
        return resolved
    except ConfigurationError:
        raise
    except (TypeError, ValueError, OSError, RuntimeError):
        pass
    raise ConfigurationError(
        "repo_path must be an accessible path-like directory"
    ) from None


def _repo_root_alias(repo_path: str | os.PathLike[str]) -> Path:
    """Return the caller-spelled absolute root used for platform path aliases.

    The canonical root is validated first by :func:`_resolve_repo_path`. This
    second projection intentionally preserves aliases such as macOS ``/var`` so
    Evidence tracking can correlate operating-system spellings without relaxing
    containment. Any unexpected path conversion failure remains a safe SDK error.
    """

    try:
        return Path(os.path.abspath(Path(repo_path).expanduser()))
    except (TypeError, ValueError, OSError, RuntimeError):
        pass
    raise ConfigurationError(
        "repo_path must be an accessible path-like directory"
    ) from None


def _resolve_trace_evidence(
    configured: TraceEvidenceCapture | None,
) -> tuple[TraceEvidenceCapture | None, str | None]:
    """Prefer explicit Trace capture, otherwise attach to compatible global OTel.

    Automatic discovery is optional and failure-isolated: inability to import or
    attach OTel becomes a safe runtime warning instead of blocking Run creation.
    """
    if configured is not None:
        if not isinstance(configured, TraceEvidenceCapture):
            raise ConfigurationError(
                "trace_evidence must come from kuma.otel.configure_trace_evidence()"
            )
        return configured, None
    try:
        from .otel import _automatic_trace_evidence
    except ImportError:
        return None, "trace_auto_capture_unavailable"
    try:
        capture = _automatic_trace_evidence()
        if capture is None:
            return None, "trace_auto_capture_unavailable"
        return capture, None
    except Exception:
        # Automatic observability cannot turn a valid Run into a failed Run.
        return None, "trace_auto_attach_failed"


def _prepare_case_requirement(
    case_provider: CaseProvider | Callable[[CaseGenerationContext], Any] | None,
    requirement_path: str | os.PathLike[str] | None,
) -> tuple[CaseProvider | None, RequirementSpec | None]:
    """Validate the Case Provider requirement precondition before side effects.

    ``create_run`` invokes this local boundary immediately after pure option
    validation. It adapts a custom Provider only far enough to read its declared
    ``requirement_required`` policy, then delegates all requirement parsing to
    :func:`parse_requirement`. Official mode is represented by a ``None``
    Provider and always requires a requirement.

    Args:
        case_provider: User-supplied Case Provider/callable, or ``None`` to select
            the official Provider.
        requirement_path: Explicit requirement file to parse. ``None`` is valid
            only when the adapted custom Provider declares
            ``requirement_required=False``.

    Returns:
        The adapted custom Provider (or ``None`` for official mode) and the parsed
        requirement (or ``None`` for an opted-out custom Provider).

    Raises:
        ConfigurationError: If a custom Provider cannot be adapted.
        ValidationError: With ``requirement_required`` when a required path is
            absent, or with the existing parser code when the selected file is
            missing or invalid.

    Preconditions:
        General ``create_run`` options have passed pure configuration validation;
        no credential, repository, Runtime, Evidence, or transport setup has run.

    Postconditions:
        Success proves the selected Provider's requirement precondition and, when
        a path was supplied, returns the one canonical parsed representation.
        Failure leaves Backend, Runtime, Evidence, and repository state untouched.

    Side Effects:
        May read only the explicitly selected requirement and its explicitly
        referenced local schema through :func:`parse_requirement`. It performs no
        credential lookup, repository scan, OTel attachment, or network request.

    Security/Privacy:
        Requirement content remains local at this stage and is never sent by this
        helper. Later official-wire allowlisting and sensitive scanning still own
        the upload boundary.
    """
    adapted = None if case_provider is None else adapt_case_provider(case_provider)
    requirement_required = adapted is None or bool(
        getattr(adapted, "requirement_required", True)
    )
    if requirement_required and requirement_path is None:
        raise ValidationError(
            "This Case Provider requires an explicit requirement_path",
            code="requirement_required",
        )
    requirement = (
        None if requirement_path is None else parse_requirement(requirement_path)
    )
    return adapted, requirement


def _adapted_providers(
    *,
    config: CreateRunConfig,
    case_provider: CaseProvider | None,
    judge_provider: Any,
    api_key: str | None,
    repo_path: Path,
    trace_evidence: TraceEvidenceCapture | None,
) -> tuple[CaseProvider, JudgeProvider | None, bool, bool]:
    """Resolve local or official Providers and negotiate safe Evidence capability.

    Official Case plus Judge may read entitlements for Evidence negotiation. An
    explicit official ``max_steps`` also requires that public read before a new
    Case operation; the provider reuses the same response when both apply.
    Private service configuration never enters the SDK.

    Args:
        config: Validated options controlling Judge, privacy, timeout, retry, and
            current Evidence capture abilities.
        case_provider: Provider already adapted by the requirement preflight, or
            ``None`` for official Case.
        judge_provider: User provider/callback, or ``None`` for official Judge.
        api_key: Optional explicit public Backend credential.
        repo_path: Canonical repository root used for safe pending-state paths.
        trace_evidence: Active in-process OTel capture, or ``None``.

    Returns:
        Adapted Case provider, optional Judge provider, and booleans identifying
        whether each side is official.

    Raises:
        ConfigurationError: If a custom Case lacks ``max_steps`` or a provider
            configuration is invalid.
        KumaError: If public capability negotiation fails.

    Preconditions:
        ``config`` and ``repo_path`` have passed local validation, and a custom
        Case Provider has already passed requirement preflight adaptation.

    Postconditions:
        Returned providers implement SDK protocols. New CaseGen capability fields
        are enabled only when the service advertises the exact framework and the
        SDK can truthfully emit observable Evidence. When already fetched, the
        entitlement response is attached to the official Case provider so its
        step-limit preflight does not duplicate the GET.

    Side Effects:
        May resolve credentials and perform one public entitlements GET. It does
        not generate a Case or run Judge.

    Security/Privacy:
        Negotiation sends no repository/requirement content and never reads or
        stores private service configuration.
    """
    official_case = case_provider is None
    official_judge = config.judge and judge_provider is None
    if not official_case and config.max_steps is None:
        raise ConfigurationError("Custom Case Providers require max_steps")

    backend = None
    if official_case or official_judge:
        backend = BackendClient(
            api_key,
            base_url=os.environ.get("KUMA_BASE_URL") or DEFAULT_BASE_URL,
            timeout=config.timeout,
            max_retries=config.max_retries,
        )
    evidence_capabilities = derive_casegen_evidence_capabilities(
        track_files=config.track_files,
        trace_evidence_configured=trace_evidence is not None,
    )
    can_negotiate = bool(official_case and official_judge and evidence_capabilities)
    entitlements = None
    if can_negotiate and backend is not None:
        entitlements = backend.json("GET", "/sdk/entitlements/")
        can_negotiate = casegen_framework_is_advertised(entitlements)
    if not can_negotiate:
        evidence_capabilities = ()
    if official_case and backend is not None:
        official_case_provider = OfficialCaseProvider(
            backend,
            allow_sensitive=config.allow_sensitive,
            operation_wait_timeout=config.operation_wait_timeout,
            max_steps=config.max_steps,
        )
        if entitlements is not None:
            official_case_provider._configure_entitlements(entitlements)
        official_case_provider._configure_evidence_capabilities(evidence_capabilities)
        adapted_case: CaseProvider = official_case_provider
    else:
        adapted_case = case_provider
    if not config.judge:
        adapted_judge = None
    elif official_judge and backend is not None:
        adapted_judge = OfficialJudgeProvider(
            backend,
            allow_sensitive=config.allow_sensitive,
            operation_wait_timeout=config.operation_wait_timeout,
            state_root=repo_path / ".kuma" / "runs",
        )
    else:
        adapted_judge = adapt_judge_provider(judge_provider)
    return adapted_case, adapted_judge, official_case, official_judge


def create_run(
    *,
    repo_path: str | os.PathLike[str] = ".",
    requirement_path: str | os.PathLike[str] | None = None,
    case_provider: Any = None,
    judge_provider: Any = None,
    strategy: str = "auto",
    max_steps: int | None = None,
    judge: bool = True,
    on_failure: str = "continue",
    allow_local: bool = False,
    track_files: bool = True,
    upload_diff: bool = False,
    save_local: bool = False,
    allow_sensitive: bool = False,
    timeout: float = 300.0,
    operation_wait_timeout: float = 600.0,
    max_retries: int = 2,
    api_key: str | None = None,
    trace_evidence: TraceEvidenceCapture | None = None,
) -> Run:
    """Create one complete Case and return its synchronous strict-handshake Run.

    Args:
        repo_path: Repository root visible to the Agent. Defaults to the current
            directory. Symlink roots, filesystem roots, and missing directories
            are rejected before runtime creation.
        requirement_path: UTF-8 requirement file used for Case generation.
            Official Case generation requires it; a custom Provider may opt out.
        case_provider: :class:`CaseProvider` or compatible callable. ``None``
            selects the official authenticated provider.
        judge_provider: :class:`JudgeProvider` or compatible callable. ``None``
            selects the official provider when ``judge=True``.
        strategy: ``"auto"`` or an explicit public strategy ID. The SDK never
            invents or silently substitutes an unknown strategy.
        max_steps: Maximum number of Case steps allowed. For example, ``3``
            permits one, two, or three inputs; it does not require exactly three.
            In official mode a value above the current service limit is rejected
            before Case generation, with the allowed maximum available in the
            stable error details; the SDK never truncates a returned Case. Custom
            Case Providers require an explicit positive value. ``None`` uses the
            official service policy.
        judge: Whether to request a final Judgment after the last Submission.
        on_failure: ``"continue"`` advances after a non-completed Submission;
            ``"stop"`` closes the Run immediately.
        allow_local: Permit a trusted non-Docker development run. This does not
            create a sandbox or relax path, privacy, and protocol validation.
        track_files: Capture bounded repository file metadata before and after
            each input.
        upload_diff: Include bounded safe text diffs. Requires
            ``track_files=True`` and may expose selected repository text to the
            configured Judge after sensitive scanning.
        save_local: Persist committed Submission records under the SDK-owned
            ``.kuma/runs/<run_id>/`` directory using atomic replacement.
        allow_sensitive: Permit ordinary Evidence that triggers the scanner in
            contracts where this opt-in is supported. It never permits secrets,
            private rubrics, or non-allowlisted OTel values.
        timeout: Positive finite timeout in seconds for each HTTP request.
        operation_wait_timeout: Positive finite total seconds allowed for one
            official asynchronous Case or Judge operation, across all polls.
        max_retries: Transient HTTP retries after the first attempt, from ``0``
            through ``5``. Retried idempotent requests reuse the same key.
        api_key: Per-call opaque ``dfx_`` credential. ``None`` resolves
            ``KUMA_API_KEY`` and then the local credential file when an official
            provider needs authentication.
        trace_evidence: Capture created by
            :func:`kuma.otel.configure_trace_evidence`. ``None`` attempts to
            reuse a compatible configured global OTel provider; unavailable OTel
            becomes a non-blocking ``runtime_warnings`` entry.

    Returns:
        A synchronous :class:`Run` in ``ready`` state. Use ``get_input`` and
        ``submit`` alternately, then inspect ``report`` after judging.

    Raises:
        KumaError: If configuration, authentication, runtime isolation, Case
            generation/validation, Evidence setup, or a public service response
            fails. Concrete subclasses expose stable ``code`` and ``retryable``.

    Preconditions:
        ``repo_path`` is the repository the caller authorizes KUMA to inspect.
        Official generation needs a readable requirement and valid credential.
        Unless ``allow_local=True``, the process runs in the supported container.
        Only one Run may own the process/container active-Run lease at a time.

    Postconditions:
        Success returns one validated Case with a non-empty input sequence no
        larger than ``max_steps`` when supplied, and retains the active lease
        until completion/cancellation. If setup fails after lease acquisition,
        runtime resources are closed before the exception escapes.

    Side Effects:
        Reads the requirement and bounded repository metadata; may create the
        repository ``.kuma`` runtime area; may call the public Backend for
        official Case generation. The caller must call ``cancel`` when abandoning
        an unfinished Run so resources are released promptly.

    Security/Privacy:
        Official providers communicate only with the public Backend. The SDK
        never contacts MCP, a model provider, or a database directly. Repository,
        Evidence, and OTel capture are bounded and sensitive-data checked. Custom
        providers execute in-process and therefore inherit caller permissions.
    """

    config = resolve_create_run_config(
        {
            "strategy": strategy,
            "max_steps": max_steps,
            "judge": judge,
            "on_failure": on_failure,
            "allow_local": allow_local,
            "track_files": track_files,
            "upload_diff": upload_diff,
            "save_local": save_local,
            "allow_sensitive": allow_sensitive,
            "timeout": timeout,
            "operation_wait_timeout": operation_wait_timeout,
            "max_retries": max_retries,
        }
    )
    if config.upload_diff and not config.track_files:
        raise ConfigurationError("upload_diff requires track_files=True")
    adapted_case_input, requirement = _prepare_case_requirement(
        case_provider,
        requirement_path,
    )
    resolved_repo = _resolve_repo_path(repo_path)
    repo_root_alias = _repo_root_alias(repo_path)
    trace_evidence, trace_auto_warning = _resolve_trace_evidence(trace_evidence)
    (
        adapted_case_provider,
        adapted_judge_provider,
        official_case,
        official_judge,
    ) = _adapted_providers(
        config=config,
        case_provider=adapted_case_input,
        judge_provider=judge_provider,
        api_key=api_key,
        repo_path=resolved_repo,
        trace_evidence=trace_evidence,
    )

    run_id = f"run_{uuid.uuid4().hex}"
    runtime = RuntimeSession.open(
        run_id=run_id,
        repo_path=resolved_repo,
        allow_local=config.allow_local,
        save_local=config.save_local,
    )
    try:
        effective_max_steps = config.max_steps or (
            DEFAULT_CASE_MAX_STEPS if official_case else 50
        )
        repo_meta = collect_repo_meta(resolved_repo)
        context = CaseGenerationContext(
            repo_path=resolved_repo,
            repo_meta=repo_meta.to_dict(),
            requirement=None if requirement is None else requirement.content,
            agent_description=(
                None if requirement is None else requirement.agent_description
            ),
            requirement_sections=({} if requirement is None else requirement.sections),
            input_type="auto" if requirement is None else requirement.input_type,
            input_schema=None if requirement is None else requirement.input_schema,
            strategy=config.strategy,
            max_steps=effective_max_steps,
        )
        try:
            raw_case = adapted_case_provider.generate_case(context)
        except KumaError:
            raise
        except Exception as exc:
            raise ProviderError("The custom Case Provider failed") from exc
        case = normalize_case(
            raw_case,
            run_id=run_id,
            max_steps=effective_max_steps,
            required_input_type=(
                None if requirement is None else requirement.input_type
            ),
            required_input_schema=(
                None if requirement is None else requirement.input_schema
            ),
        )
        official_provenance = case.extensions.get("official_case")
        if official_case:
            validate_official_case_provenance(official_provenance)
        elif "official_case" in case.extensions:
            raise ProviderError(
                "Custom Case Provider returned reserved official_case metadata",
                code="invalid_case_provenance",
            )
        if (
            not official_case
            and config.judge
            and not official_judge
            and case.rubric is None
        ):
            raise ConfigurationError(
                "Custom Case + custom Judge requires a fixed public rubric"
            )
        evidence = EvidenceCollector(
            root=resolved_repo,
            root_alias=repo_root_alias,
            scope="container" if runtime.mode == "docker" else "local",
            excluded_roots=(
                runtime.workspace.runtime_root,
                resolved_repo / ".kuma",
            ),
            track_files=config.track_files,
            upload_diff=config.upload_diff,
            save_local=config.save_local,
            allow_sensitive=config.allow_sensitive,
            block_sensitive=official_judge or trace_evidence is not None,
            persistent_path=runtime.workspace.persistent_path,
            run_id=run_id,
            case_id=case.case_id,
            trace_evidence=trace_evidence,
        )
        if trace_auto_warning is not None:
            evidence.runtime_warnings.append(trace_auto_warning)
        return Run(
            run_id=run_id,
            case=case,
            runtime=runtime,
            judge_provider=adapted_judge_provider,
            judge_enabled=config.judge,
            on_failure=config.on_failure,
            strategy=config.strategy,
            evidence=evidence,
        )
    except BaseException:
        runtime.close()
        raise


__all__ = ["configure", "create_run"]
