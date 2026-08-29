"""Top-level v4 API entry points."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from .config import CreateRunConfig, resolve_create_run_config, write_api_key
from .contracts import Case
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

    The function returns the written path and performs no network request.
    ``KUMA_CONFIG_HOME`` can redirect the user-level location for isolated
    development and tests.
    """

    return write_api_key(api_key)


def _resolve_trace_evidence(
    configured: TraceEvidenceCapture | None,
) -> tuple[TraceEvidenceCapture | None, str | None]:
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
        # Optional observability must never turn a valid Run into a failed Run.
        return None, "trace_auto_attach_failed"


def _adapted_providers(
    *,
    config: CreateRunConfig,
    case_provider: Any,
    judge_provider: Any,
    api_key: str | None,
    repo_path: Path,
    trace_evidence: TraceEvidenceCapture | None,
) -> tuple[CaseProvider, JudgeProvider | None, bool, bool]:
    official_case = case_provider is None
    official_judge = config.judge and judge_provider is None
    if not official_case and config.max_inputs is None:
        raise ConfigurationError("Custom Case Providers require max_inputs")

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
    if can_negotiate and backend is not None:
        can_negotiate = casegen_framework_is_advertised(
            backend.json("GET", "/sdk/entitlements/")
        )
    if not can_negotiate:
        evidence_capabilities = ()
    if official_case and backend is not None:
        official_case_provider = OfficialCaseProvider(
            backend,
            allow_sensitive=config.allow_sensitive,
            operation_wait_timeout=config.operation_wait_timeout,
        )
        official_case_provider._configure_evidence_capabilities(evidence_capabilities)
        adapted_case: CaseProvider = official_case_provider
    else:
        adapted_case = adapt_case_provider(case_provider)
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


def _create_run_config(
    *,
    strategy: str,
    max_inputs: int | None,
    judge: bool,
    on_failure: str,
    allow_local: bool,
    track_files: bool,
    upload_diff: bool,
    save_local: bool,
    allow_sensitive: bool,
    timeout: float,
    operation_wait_timeout: float,
    max_retries: int,
) -> CreateRunConfig:
    config = resolve_create_run_config(
        {
            "strategy": strategy,
            "max_inputs": max_inputs,
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
    return config


def _case_generation_context(
    *,
    resolved_repo: Path,
    requirement_path: str | os.PathLike[str] | None,
    config: CreateRunConfig,
) -> tuple[RequirementSpec | None, CaseGenerationContext]:
    requirement = (
        None if requirement_path is None else parse_requirement(requirement_path)
    )
    repo_meta = collect_repo_meta(resolved_repo)
    context = CaseGenerationContext(
        repo_path=resolved_repo,
        repo_meta=repo_meta.to_dict(),
        requirement=None if requirement is None else requirement.content,
        agent_description=None
        if requirement is None
        else requirement.agent_description,
        requirement_sections={} if requirement is None else requirement.sections,
        input_type="auto" if requirement is None else requirement.input_type,
        input_schema=None if requirement is None else requirement.input_schema,
        strategy=config.strategy,
        max_inputs=config.max_inputs or 50,
    )
    return requirement, context


def _generated_case(
    *,
    provider: CaseProvider,
    context: CaseGenerationContext,
    requirement: RequirementSpec | None,
    run_id: str,
    config: CreateRunConfig,
) -> Case:
    try:
        raw_case = provider.generate_case(context)
    except KumaError:
        raise
    except Exception as exc:
        raise ProviderError("The custom Case Provider failed") from exc
    return normalize_case(
        raw_case,
        run_id=run_id,
        max_inputs=config.max_inputs or 50,
        required_input_type=None if requirement is None else requirement.input_type,
        required_input_schema=(
            None if requirement is None else requirement.input_schema
        ),
    )


def _validate_case_provenance_and_rubric(
    *,
    case: Case,
    official_case: bool,
    official_judge: bool,
    config: CreateRunConfig,
) -> None:
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


def _evidence_collector(
    *,
    runtime: RuntimeSession,
    resolved_repo: Path,
    run_id: str,
    case: Case,
    config: CreateRunConfig,
    official_judge: bool,
    trace_evidence: TraceEvidenceCapture | None,
) -> EvidenceCollector:
    tracking_root = Path("/") if runtime.mode == "docker" else resolved_repo
    return EvidenceCollector(
        root=tracking_root,
        scope="container" if runtime.mode == "docker" else "local",
        excluded_roots=(runtime.workspace.runtime_root, resolved_repo / ".kuma"),
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


def _assemble_run(
    *,
    runtime: RuntimeSession,
    resolved_repo: Path,
    requirement_path: str | os.PathLike[str] | None,
    run_id: str,
    config: CreateRunConfig,
    case_provider: CaseProvider,
    judge_provider: JudgeProvider | None,
    official_case: bool,
    official_judge: bool,
    trace_evidence: TraceEvidenceCapture | None,
    trace_auto_warning: str | None,
) -> Run:
    requirement, context = _case_generation_context(
        resolved_repo=resolved_repo,
        requirement_path=requirement_path,
        config=config,
    )
    case = _generated_case(
        provider=case_provider,
        context=context,
        requirement=requirement,
        run_id=run_id,
        config=config,
    )
    _validate_case_provenance_and_rubric(
        case=case,
        official_case=official_case,
        official_judge=official_judge,
        config=config,
    )
    evidence = _evidence_collector(
        runtime=runtime,
        resolved_repo=resolved_repo,
        run_id=run_id,
        case=case,
        config=config,
        official_judge=official_judge,
        trace_evidence=trace_evidence,
    )
    if trace_auto_warning is not None:
        evidence.runtime_warnings.append(trace_auto_warning)
    return Run(
        run_id=run_id,
        case=case,
        runtime=runtime,
        judge_provider=judge_provider,
        judge_enabled=config.judge,
        on_failure=config.on_failure,
        strategy=config.strategy,
        evidence=evidence,
    )


def _open_run(
    *,
    resolved_repo: Path,
    requirement_path: str | os.PathLike[str] | None,
    config: CreateRunConfig,
    case_provider: CaseProvider,
    judge_provider: JudgeProvider | None,
    official_case: bool,
    official_judge: bool,
    trace_evidence: TraceEvidenceCapture | None,
    trace_auto_warning: str | None,
) -> Run:
    run_id = f"run_{uuid.uuid4().hex}"
    runtime = RuntimeSession.open(
        run_id=run_id,
        repo_path=resolved_repo,
        allow_local=config.allow_local,
        save_local=config.save_local,
    )
    try:
        return _assemble_run(
            runtime=runtime,
            resolved_repo=resolved_repo,
            requirement_path=requirement_path,
            run_id=run_id,
            config=config,
            case_provider=case_provider,
            judge_provider=judge_provider,
            official_case=official_case,
            official_judge=official_judge,
            trace_evidence=trace_evidence,
            trace_auto_warning=trace_auto_warning,
        )
    except BaseException:
        runtime.close()
        raise


def create_run(
    *,
    repo_path: str | os.PathLike[str] = ".",
    requirement_path: str | os.PathLike[str] | None = None,
    case_provider: Any = None,
    judge_provider: Any = None,
    strategy: str = "auto",
    max_inputs: int | None = None,
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

    Official Providers use the deployed authenticated Backend endpoints. Custom
    Providers remain entirely local unless paired with an official Provider.
    Runtime, requirement, repository metadata, Case output, and Provider
    combinations are validated before the first Input can be delivered.

    ``allow_local`` is an explicit development escape hatch; the default runtime
    requires SDK and Agent to share a Docker container. When ``trace_evidence``
    is omitted, an already-configured global OpenTelemetry SDK provider is
    attached automatically; missing or unconfigured OTel only records a runtime
    warning. Passing a capture from :func:`kuma.otel.configure_trace_evidence`
    overrides automatic discovery.

    The caller owns Agent execution and must alternate :meth:`Run.get_input`
    with exactly one :meth:`Run.submit` until no Inputs remain, or call
    :meth:`Run.cancel` when abandoning the Run.
    """

    config = _create_run_config(
        strategy=strategy,
        max_inputs=max_inputs,
        judge=judge,
        on_failure=on_failure,
        allow_local=allow_local,
        track_files=track_files,
        upload_diff=upload_diff,
        save_local=save_local,
        allow_sensitive=allow_sensitive,
        timeout=timeout,
        operation_wait_timeout=operation_wait_timeout,
        max_retries=max_retries,
    )
    trace_evidence, trace_auto_warning = _resolve_trace_evidence(trace_evidence)
    resolved_repo = Path(repo_path).expanduser().resolve()
    (
        adapted_case_provider,
        adapted_judge_provider,
        official_case,
        official_judge,
    ) = _adapted_providers(
        config=config,
        case_provider=case_provider,
        judge_provider=judge_provider,
        api_key=api_key,
        repo_path=resolved_repo,
        trace_evidence=trace_evidence,
    )
    requirement_required = bool(
        getattr(adapted_case_provider, "requirement_required", True)
    )
    if requirement_required and requirement_path is None:
        raise ValidationError(
            "This Case Provider requires an explicit requirement_path",
            code="requirement_required",
        )
    return _open_run(
        resolved_repo=resolved_repo,
        requirement_path=requirement_path,
        config=config,
        case_provider=adapted_case_provider,
        judge_provider=adapted_judge_provider,
        official_case=official_case,
        official_judge=official_judge,
        trace_evidence=trace_evidence,
        trace_auto_warning=trace_auto_warning,
    )


__all__ = ["configure", "create_run"]
