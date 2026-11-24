"""Opt-in normal Agent capture, durable local export and explicit hosted process review."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .capture_instrumentation import prepare_instrumentation, validate_framework
from .capture_privacy import redact_capture_json
from .capture_store import (
    capture_directory,
    encoded,
    load_capture,
    output_target,
    preflight_capture_write,
    save_capture,
    save_review,
)
from .client import KumaClient
from .errors import ConfigurationError, ProviderError, ValidationError
from .evidence.trace import TraceEvidenceLimits
from .observation import observe
from .trajectory_capture import prepare_observation
from .trajectory_requirements import requirements_template, validate_requirements


def _profile(task: str, tools: Any, requirements: dict | None) -> dict:
    """Bind the recorded task and declared rules without inventing evaluation facts.

    Capture-specific credential/known-PII projection changes only the saved
    profile, never the Agent's original input. Advanced requirements remain
    authoritative and must match the projected task; a mismatch fails locally.
    """
    if not isinstance(task, str) or not task.strip() or len(task) > 12000:
        raise ValidationError(
            "Capture task must be nonblank text of at most 12000 characters"
        )
    safe, redacted = redact_capture_json(task)
    if requirements is not None:
        profile = validate_requirements(requirements)
        if tools is not None or profile["objective"] != safe:
            raise ValidationError(
                "Advanced requirements must match the original task; do not also pass tools"
            )
        return profile
    profile = requirements_template()
    profile.update(name="Recorded Agent task", version="1", objective=safe, tools=tools)
    profile["success_criteria"] = [
        {
            "id": "original-task",
            "description": "Fulfill the original task recorded in objective.",
            "priority": "required",
            "evidence_expected": [],
        }
    ]
    profile["limitations"] = [
        "Only the original task is supplied as a criterion; no independent expected result is supplied.",
        "Capture covers context-propagated instrumented calls only; other activity is unknown.",
    ]
    if redacted:
        profile["limitations"].append(
            "Recognized sensitive text was redacted from the original task."
        )
    return validate_requirements(profile)


class CaptureSession:
    """One-shot sync/async execution window with automatic redacted local persistence.

    Construct with capture(). It never runs an Agent, uploads or evaluates. Optional
    process hooks are explicit; original observe() remains non-persistent. On exit,
    save failure is raised only when it cannot mask an Agent exception/cancellation.
    persistence_status/error expose such failures without raw OS or Agent messages.
    """

    def __init__(
        self,
        task: str,
        *,
        framework: str = "existing",
        tracer_provider: Any = None,
        repo_path: str | os.PathLike = ".",
        storage_path: str | os.PathLike | None = None,
        tools: list[dict] | None = None,
        requirements: dict | None = None,
        limits: TraceEvidenceLimits | None = None,
    ) -> None:
        """Construct a one-shot local capture; prefer the capture() factory.

        Args:
            task: Original nonblank task, 1..12000 characters; recognized secrets are redacted.
            framework: existing, openai, langchain or langgraph; default existing preserves wiring.
            tracer_provider: Application-owned provider or None for the selected integration.
            repo_path: Existing workspace path, default current directory; never scanned.
            storage_path: Existing disjoint external parent, or None for ~/.kuma-captures.
            tools: Complete declared tool policies, None for unknown or [] for no tools.
            requirements: Advanced v1 rules matching the task; mutually exclusive with tools.
            limits: TraceEvidenceLimits or None for observation defaults; at least 2048 bytes.
        Returns:
            Inactive CaptureSession with stable capture_id and persistence_status=not_saved.
        Raises:
            ValidationError: Invalid task or declared rules, without running the Agent.
            ConfigurationError: Unsupported framework or invalid limits.
        Side Effects:
            Allocates local state only. Context entry acquires storage/instrumentation;
            exit finalizes and saves. No credentials, network, upload or Judge request.
        """
        validate_framework(framework)
        self._requirements = _profile(task, tools, requirements)
        self._framework, self._provider = framework, tracer_provider
        self._repo_path, self._storage_path = repo_path, storage_path
        if limits is not None and (
            not isinstance(limits, TraceEvidenceLimits) or limits.max_total_bytes < 2048
        ):
            raise ConfigurationError("Observation limits require at least 2048 bytes")
        self._limits = limits
        self._identifier = "cap_" + uuid.uuid4().hex
        self._directory: Path | None = None
        self._document: dict | None = None
        self._state = "new"
        self.persistence_status = "not_saved"
        self.persistence_error: str | None = None

    @property
    def capture_id(self) -> str:
        """Return the stable opaque local capture identifier.

        Returns:
            str of cap_ plus 32 lowercase hexadecimal characters; available before entry.
            A durable CLI lookup works only after successful persistence.
        Side Effects:
            None; does not read storage or prove the capture has been saved.
        """
        return self._identifier

    def __enter__(self) -> CaptureSession:
        """Enter this one-shot observation after local storage preflight.

        Returns:
            This CaptureSession, active for context-propagated instrumented Agent calls.
        Raises:
            ConfigurationError: Re-entry, unsafe/unwritable storage, unavailable integration
                or observation setup failure. A failed entry cannot be retried on this session.
        Side Effects:
            Creates capture directories as needed, probes writes and attaches explicit
            instrumentation before observation. No Agent execution or KUMA network request.
            The caller must exit the context; application-owned providers are not shut down.
        """
        if self._state != "new":
            raise ConfigurationError("A capture session can only be entered once")
        self._state = "entering"
        self._directory = capture_directory(
            self._repo_path, self._storage_path, create=True
        )
        preflight_capture_write(self._directory)
        provider, self._instrumentation = prepare_instrumentation(
            self._framework, self._provider
        )
        self._observation = observe(tracer_provider=provider, limits=self._limits)
        self._observation.__enter__()
        self._state = "active"
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        """Finalize observation and persist redacted facts without hiding an Agent failure.

        Args:
            exc_type: Exception class supplied by with, or None for normal completion.
            exc: Original exception instance or None; not copied into public error messages.
            traceback: Original traceback or None, forwarded to the observation context.
        Returns:
            False in every normal return, so an original Agent exception is not suppressed.
        Raises:
            ConfigurationError: Finalization/save failed on normal exit; inspect
                persistence_status/error and export() for recoverable finalized data.
        Side Effects:
            Closes the observation and attempts one local save; marks persistence saved
            or failed. If an Agent exception/cancellation already exists, preserves it
            instead of raising the save error. Repeated exit is a no-op; no upload occurs.
        """
        if self._state != "active":
            return False
        self._state = "closed"
        failed = False
        try:
            self._observation.__exit__(exc_type, exc, traceback)
            self._document = {
                "schema_version": "kuma.capture.v1",
                "capture_id": self.capture_id,
                "requirements": self._requirements,
                "observation": self._observation.export(),
                "instrumentation": self._instrumentation,
            }
            save_capture(self._directory, self._document)
            self.persistence_status = "saved"
        except Exception:
            failed = True
            self.persistence_status, self.persistence_error = (
                "failed",
                "capture_save_failed",
            )
        if failed and exc_type is None:
            raise ConfigurationError(
                "Capture could not be persisted; inspect session persistence_status and export()"
            )
        return False

    async def __aenter__(self) -> CaptureSession:
        """Enter the same capture lifecycle from async with.

        Returns:
            This active CaptureSession, with the same one-shot semantics as __enter__.
        Raises:
            ConfigurationError: The same storage/instrumentation/entry failures as __enter__.
        Side Effects:
            Performs synchronous local setup on the calling thread; adds no background task.
            Application-owned providers remain owned by the caller.
        """
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        """Finalize on async exit while retaining the original cancellation identity.

        Args:
            exc_type: Exception class from async with, or None on normal completion.
            exc: Original exception/cancellation instance or None; never rewritten.
            traceback: Original traceback or None passed to observation finalization.
        Returns:
            False, preserving exception propagation as in __exit__.
        Raises:
            ConfigurationError: Local save/finalization failure on otherwise normal exit.
        Side Effects:
            Synchronously finalizes and saves on the calling thread, without scheduling
            background work. On cancellation, save failure is exposed through persistence
            status/error rather than replacing that cancellation. No upload or Judge call.
        """
        return self.__exit__(exc_type, exc, traceback)

    def export(self) -> dict:
        """Return detached finalized capture data, including task, gaps and actual execution status.

        Returns:
            Detached kuma.capture.v1 dict containing the task, observation, instrumentation
            and capture_id; may be available even when the local save failed.
        Raises:
            ConfigurationError: Finalization has not produced a document; no synthetic
                successful capture is returned.
        Side Effects:
            None; reads only finalized in-memory state, with no I/O or upload.
        """
        if self._document is None:
            raise ConfigurationError("Finish capture before exporting its data")
        return copy.deepcopy(self._document)


def capture(
    task: str,
    *,
    framework: str = "existing",
    tracer_provider: Any = None,
    repo_path: str | os.PathLike = ".",
    storage_path: str | os.PathLike | None = None,
    tools: list[dict] | None = None,
    requirements: dict | None = None,
    limits: TraceEvidenceLimits | None = None,
) -> CaptureSession:
    """Record normal Agent execution and save locally; never implicitly upload or Judge.

    Args:
        task: Actual original task, nonblank1..12000 characters; recognized secrets
            are redacted before persistence, with a limitation. The Agent input is
            caller-owned and is never mutated. No success/reference is invented.
        framework: existing (default) observes application-owned OTel wiring;
            openai/langchain/langgraph explicitly enable the supported optional hook.
        tracer_provider: Existing SDK-compatible provider, preserved as-is. None
            reuses compatible global/established KUMA wiring, or creates a private
            no-exporter provider for an explicit selected integration.
        repo_path: Existing Agent workspace, default current directory; no scanning.
        storage_path: Existing external parent, disjoint from workspace, or None
            for ~/.kuma-captures. Default root and .kuma/captures are created on entry.
        tools: Optional complete v1 tool policies; None means unknown, [] declares
            no tools. No tool catalog is inferred from observed calls.
        requirements: Optional complete advanced v1 profile with objective equal to
            the original task; mutually exclusive with tools. No JSON file required.
        limits: TraceEvidenceLimits or None for existing observation limits. Omissions
            and zero capture remain visible; no hidden whole-field truncation.
    Returns:
        One-shot CaptureSession, supporting with/async with and stable capture_id.
    Raises:
        ValidationError: Invalid task/rules. ConfigurationError: Unsupported or
            unavailable explicit integration, unsafe storage or normal-exit save failure.
    Side Effects:
        Explicit optional process instrumentation and local redacted persistence on
        exit. No key, model request, network, Case, Judge or credit consumption. User
        exporters retain their independent behavior. Agent exceptions/cancellation
        propagate; inspect persistence_status/error if saving also fails.
    """
    return CaptureSession(
        task,
        framework=framework,
        tracer_provider=tracer_provider,
        repo_path=repo_path,
        storage_path=storage_path,
        tools=tools,
        requirements=requirements,
        limits=limits,
    )


def export_capture(
    identifier: str,
    path: str | os.PathLike,
    *,
    repo_path: str | os.PathLike = ".",
    storage_path: str | os.PathLike | None = None,
) -> Path:
    """Load one durable ID and atomically export its task-bound document without overwrite.

    Args:
        identifier: Durable cap_ plus 32 lowercase hex ID from a saved session.
        path: New destination path with an existing parent; existing targets are rejected.
        repo_path: Existing original workspace path, default current directory.
        storage_path: Original disjoint external parent or None for ~/.kuma-captures.
    Returns:
        Absolute pathlib.Path of the atomically published task-bound JSON document.
    Raises:
        KumaError: Invalid/missing capture, unsafe paths, malformed content or local I/O
            failure; messages do not expose raw OS details.
    Side Effects:
        Bounded local read and atomic no-overwrite write; pinned handles are released.
        Partial/failed capture status is preserved. No credentials, network or billing.
    """
    document, _ = load_capture(
        identifier, repo_path=repo_path, storage_path=storage_path
    )
    with output_target(path) as publish:
        publish(document)
    return Path(path).expanduser().absolute()


def _review_inputs(
    document: dict, requirements: dict | None, key: str | None
) -> tuple[dict, str]:
    """Validate source/profile/key before uploads and derive a stable logical review identity."""
    if key is not None and (
        not isinstance(key, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", key) is None
    ):
        raise ValidationError("A valid trajectory idempotency key is required")
    profile = (
        document["requirements"]
        if requirements is None
        else validate_requirements(requirements)
    )
    if profile["objective"] != document["requirements"]["objective"]:
        raise ValidationError(
            "Review requirements must retain the original captured task"
        )
    if not document["observation"]["spans"]:
        raise ValidationError(
            "No spans retained; fix instrumentation before requesting Judge"
        )
    submission = prepare_observation(document["observation"], profile)
    logical = (
        key
        or "capture-"
        + hashlib.sha256(
            encoded({"capture_id": document["capture_id"], "submission": submission})
        ).hexdigest()
    )
    return submission, logical


def judge_capture(
    identifier: str,
    *,
    repo_path: str | os.PathLike = ".",
    storage_path: str | os.PathLike | None = None,
    client: KumaClient | None = None,
    requirements: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Explicitly review a saved execution using one Kuma credential and the bound task.

    Args:
        identifier: Durable cap_ ID in the selected local store.
        repo_path: Original workspace, default current directory.
        storage_path: Original external storage parent, or None for the default.
        client: KumaClient or None to resolve KUMA_API_KEY/normal credentials. No
            provider/storage credential is needed; Agent runtime keys remain separate.
        requirements: Optional richer v1 rules retaining the exact original task.
        idempotency_key: Optional8..200 ASCII protocol key. None hashes immutable
            capture/submission content. Same input/key recovers the same review;
            selecting a fresh key deliberately requests a separate paid review.
    Returns:
        Validated completed/stopped public review, also durably retained locally.
    Raises:
        KumaError: Invalid/unavailable local capture, empty evidence, auth/scopes,
            credits, unknown timeout outcome, remote failure or local save failure.
    Side Effects:
        One explicit upload then one Judge request, no retry or Agent rerun. Existing
        configured fixed Judge fee applies only to completed validated review. A
        local result-save failure does not undo a completed server review: recover
        with identical inputs/key. No original trace is rewritten or deleted.
    """
    document, directory = load_capture(
        identifier, repo_path=repo_path, storage_path=storage_path
    )
    submission, key = _review_inputs(document, requirements, idempotency_key)
    preflight_capture_write(directory)
    workspace = (KumaClient() if client is None else client).trajectory()
    uploaded = workspace.upload(submission)
    review = workspace.evaluate(uploaded["id"], idempotency_key=key)
    if review["requirements_sha256"] != submission["requirements_sha256"]:
        raise ProviderError("Review does not match the submitted task and rules")
    save_review(directory, review)
    return review
