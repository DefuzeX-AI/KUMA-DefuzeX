"""Official Case Provider for the Website-backend public API."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ..config import DEFAULT_CASE_MAX_STEPS
from ..errors import (
    ConfigurationError,
    LimitExceededError,
    ProviderError,
    ValidationError,
)
from ..evidence.runtime_contract import CASEGEN_EVIDENCE_CAPABILITY_ORDER
from ..repository.metadata import prepare_repo_meta_upload
from ..repository.privacy import enforce_sensitive_policy, scan_sensitive_json
from ..repository.strategy_groups import validate_strategy_group_wire_selection
from ..transport.backend import BackendClient, new_idempotency_key
from ..transport.operations import PendingOperationStore, await_operation
from ..transport.request_records import (
    RequestOperationStore,
    canonical_request_sha256,
)
from ._official_wire import (
    canonical_sha256,
    contains_private_fields,
    required_text,
    validate_official_case_provenance,
)
from .base import CaseGenerationContext

_BEHAVIOR_SPEC_FIELDS = (
    "production_scenario",
    "behaviors_to_test",
    "prohibited_behaviors",
)
_MAX_BEHAVIOR_FIELD_CHARS = 4000
_MAX_BEHAVIOR_FIELD_BYTES = 8 * 1024
_MAX_BEHAVIOR_SPEC_BYTES = 16 * 1024


def _client_credential_identity(client: Any) -> str:
    """Return the real key digest or a stable identity for controlled test clients."""
    value = getattr(client, "credential_identity", None)
    if isinstance(value, str) and len(value) == 64:
        return value
    return hashlib.sha256(b"kuma-controlled-provider-client").hexdigest()


def _casegen_max_steps_limit(entitlements: Mapping[str, Any]) -> int:
    """Return the closed public Case ceiling, using 10 for legacy omission.

    Args:
        entitlements: Validated top-level response from ``GET /sdk/entitlements/``.

    Returns:
        Integer service ceiling from 1 through 10. Older responses may omit only
        ``casegen_max_steps`` and therefore use the historical default of 10.

    Raises:
        ProviderError: If ``limits`` or the advertised ceiling has an invalid
            public shape.
    """
    limits = entitlements.get("limits")
    if not isinstance(limits, Mapping):
        raise ProviderError(
            "The Backend returned invalid Case generation limits",
            code="invalid_response",
        )
    value = limits.get("casegen_max_steps", DEFAULT_CASE_MAX_STEPS)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= DEFAULT_CASE_MAX_STEPS
    ):
        raise ProviderError(
            "The Backend returned invalid Case generation limits",
            code="invalid_response",
        )
    return value


def _validate_requested_max_steps(
    entitlements: Mapping[str, Any], requested: int
) -> None:
    """Reject an explicit Case ceiling above the service limit before POST.

    Args:
        entitlements: Current public entitlement response for this API key.
        requested: Positive step ceiling explicitly supplied by the caller.

    Raises:
        ProviderError: If the service-limit response is malformed.
        LimitExceededError: If ``requested`` exceeds the advertised ceiling;
            ``details["max_allowed_steps"]`` contains the safe integer maximum.

    Postconditions:
        Success proves the request may be posted under the observed limit. A
        rejection occurs before an operation, credit reservation, or model call.

    Side Effects:
        None. The caller owns the entitlement GET and subsequent Case POST.
    """
    allowed = _casegen_max_steps_limit(entitlements)
    if requested > allowed:
        raise LimitExceededError(
            f"max_steps exceeds the Case service limit; maximum allowed is {allowed}.",
            code="case_step_limit_exceeded",
            details={"max_allowed_steps": allowed},
        )


def _official_inputs(steps: Any, *, max_steps: int) -> list[dict[str, str]]:
    """Validate bounded public Case steps and map them to text Input payloads."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= max_steps:
        raise ProviderError(
            "The Backend returned an invalid number of Case steps",
            code="invalid_response",
        )
    inputs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for step in steps:
        if not isinstance(step, Mapping):
            raise ProviderError(
                "The Backend returned an invalid Case step", code="invalid_response"
            )
        step_id = required_text(step.get("step_id"), "step_id")
        if step_id in seen_ids:
            raise ProviderError(
                "The Backend returned duplicate step IDs", code="invalid_response"
            )
        seen_ids.add(step_id)
        inputs.append(
            {
                "input_id": step_id,
                "payload_type": "text",
                "payload": required_text(step.get("prompt"), "step prompt"),
            }
        )
    return inputs


def _case_matches_request(
    raw_case: Mapping[str, Any],
    batch: Mapping[str, Any],
    *,
    case_id: str,
    batch_id: str,
    strategy_id: str,
    strategy_version: str,
    repo_fingerprint: str,
) -> bool:
    """Check batch, strategy, repository, and signature metadata as one integrity unit."""
    returned_fingerprint = str(raw_case.get("repo_fingerprint", ""))
    return (
        batch.get("case_ids") == [case_id]
        and batch.get("strategy_id") == strategy_id
        and batch.get("strategy_version") == strategy_version
        and raw_case.get("batch_id") == batch_id
        and raw_case.get("strategy_id") == strategy_id
        and raw_case.get("strategy_version") == strategy_version
        and returned_fingerprint.removeprefix("sha256:").lower() == repo_fingerprint
        and isinstance(raw_case.get("signature"), str)
        and bool(raw_case["signature"])
    )


def _official_case_response(
    response: Mapping[str, Any],
    *,
    max_steps: int,
    requested_strategy_id: str,
    requested_strategy_version: str | None,
    requested_strategy_group: Mapping[str, Any] | None,
    repo_fingerprint: str,
) -> tuple[str, str, str, str, Mapping[str, Any], list[dict[str, str]]]:
    """Validate one official Case without conflating Group and member identities.

    Args:
        response: Terminal public Backend Case result.
        max_steps: Maximum complete public steps accepted for this request.
        requested_strategy_id: Strategy requested on the no-Group path, or
            ``auto`` when the Backend selects an actual member.
        requested_strategy_version: Exact no-Group strategy version when the
            caller supplied one; current SDK Case requests normally omit it.
        requested_strategy_group: Closed resolved Group coordinate sent in the
            request, or ``None`` for the supported no-Group fallback path.
        repo_fingerprint: Canonical repository metadata digest sent upstream.

    Returns:
        Case and batch IDs, returned member strategy coordinate, raw public
        Case, and normalized Inputs. Group provenance is validated separately
        and is not fabricated when the compatibility response omits it.

    Raises:
        ProviderError: If the public envelope, member binding, optional Group
            provenance, fingerprint, signature, or step contract is invalid.

    Postconditions:
        The member strategy is internally consistent between batch and Case.
        No-Group explicit requests additionally match that member. Group
        requests are compared only with independent Group provenance when the
        server supplies it; member IDs are never treated as Group IDs.
    """
    batch, raw_case = _case_response_envelope(response)
    case_id = required_text(raw_case.get("case_id"), "case_id")
    batch_id = required_text(batch.get("batch_id"), "batch_id")
    _validate_response_strategy_group(
        response,
        batch,
        raw_case,
        requested=requested_strategy_group,
    )
    strategy_id, strategy_version = _selected_strategy(
        batch,
        requested_strategy_id=requested_strategy_id,
        requested_strategy_version=requested_strategy_version,
        enforce_requested_identity=requested_strategy_group is None,
    )
    if not _case_matches_request(
        raw_case,
        batch,
        case_id=case_id,
        batch_id=batch_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        repo_fingerprint=repo_fingerprint,
    ):
        raise ProviderError(
            "The Backend Case integrity metadata does not match the request",
            code="invalid_response",
        )
    return (
        case_id,
        batch_id,
        strategy_id,
        strategy_version,
        raw_case,
        _official_inputs(raw_case.get("steps"), max_steps=max_steps),
    )


def _case_response_envelope(
    response: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Require one public Case and reject private fields in the Backend envelope."""
    batch = response.get("batch")
    cases = response.get("cases")
    if (
        not isinstance(batch, Mapping)
        or not isinstance(cases, list)
        or len(cases) != 1
        or contains_private_fields(response)
    ):
        raise ProviderError(
            "The Backend returned an invalid Case batch", code="invalid_response"
        )
    raw_case = cases[0]
    if not isinstance(raw_case, Mapping) or contains_private_fields(raw_case):
        raise ProviderError(
            "The Backend returned an invalid or private Case payload",
            code="invalid_response",
        )
    return batch, raw_case


def _selected_strategy(
    batch: Mapping[str, Any],
    *,
    requested_strategy_id: str,
    requested_strategy_version: str | None,
    enforce_requested_identity: bool = True,
) -> tuple[str, str]:
    """Validate the returned legacy/member strategy coordinate.

    ``strategy_id`` and ``strategy_version`` in the current Case response name
    the actual strategy member selected inside a Strategy Group. On the
    supported no-Group path, an explicit strategy still names that identity.

    Args:
        batch: Public batch metadata containing the actual member coordinate.
        requested_strategy_id: Requested no-Group strategy ID or ``auto``.
        requested_strategy_version: Optional no-Group requested version.
        enforce_requested_identity: Whether a no-Group explicit request must
            equal the returned member. Group-based requests set this to false.

    Returns:
        Non-``auto`` member strategy ID and version from the public batch.

    Raises:
        ProviderError: If the member coordinate is missing, unresolved, or does
            not match an enforced no-Group explicit request.
    """
    selected_strategy_id = required_text(
        batch.get("strategy_id"), "selected strategy_id"
    )
    selected_strategy_version = required_text(
        batch.get("strategy_version"), "selected strategy_version"
    )
    if selected_strategy_id == "auto":
        raise ProviderError(
            "The Backend did not return an actual Case strategy selection",
            code="invalid_response",
        )
    if enforce_requested_identity and (
        requested_strategy_id != "auto"
        and selected_strategy_id != requested_strategy_id
    ):
        raise ProviderError(
            "The Backend Case strategy does not match the explicit request",
            code="invalid_response",
        )
    if enforce_requested_identity and (
        requested_strategy_version is not None
        and selected_strategy_version != requested_strategy_version
    ):
        raise ProviderError(
            "The Backend Case strategy version does not match the request",
            code="invalid_response",
        )
    return selected_strategy_id, selected_strategy_version


def _validate_response_strategy_group(
    response: Mapping[str, Any],
    batch: Mapping[str, Any],
    raw_case: Mapping[str, Any],
    *,
    requested: Mapping[str, Any] | None,
) -> None:
    """Validate optional independent Strategy Group provenance from the server.

    The deployed compatibility response omits this field. A future response may
    place the same closed ``strategy_group_selection`` object on the response,
    batch, or Case while rolling the field through the public layers. Every
    present copy must be valid and exactly match the request; absence is accepted
    but is never represented locally as server-returned provenance.

    Args:
        response: Complete terminal Backend response.
        batch: Validated public batch mapping.
        raw_case: The single validated public Case mapping.
        requested: Closed Group selection sent in the request, or ``None`` for
            the supported no-Group fallback path.

    Returns:
        ``None`` after accepting either an omitted compatibility field or exact
        closed Group provenance.

    Raises:
        ProviderError: If Group provenance is unexpected, malformed,
            inconsistent across response levels, or differs from the request.

    Security/Privacy:
        Only the five public selection fields are accepted. Catalog payloads or
        private Core selection metadata cannot enter Case extensions.
    """
    returned = [
        container["strategy_group_selection"]
        for container in (response, batch, raw_case)
        if "strategy_group_selection" in container
    ]
    if not returned:
        return
    if requested is None:
        raise ProviderError(
            "The Backend returned unexpected Strategy Group provenance",
            code="invalid_response",
        )
    try:
        expected = validate_strategy_group_wire_selection(requested)
        validated = [
            validate_strategy_group_wire_selection(value) for value in returned
        ]
    except ValidationError:
        raise ProviderError(
            "The Backend returned invalid Strategy Group provenance",
            code="invalid_response",
        ) from None
    if any(value != expected for value in validated[1:]) or validated[0] != expected:
        raise ProviderError(
            "The Backend Strategy Group provenance does not match the request",
            code="invalid_response",
        )


def _agent_description(context: CaseGenerationContext) -> str:
    """Return only bounded front-matter Agent description, never Profile prose."""
    value = (
        context.agent_description.strip()
        if isinstance(context.agent_description, str)
        else ""
    )
    if len(value) > 2000:
        raise LimitExceededError(
            "The official Case service accepts at most 2000 characters in agent_description",
            code="agent_description_too_large",
        )
    return value


def _behavior_spec(context: CaseGenerationContext) -> dict[str, str]:
    """Validate the three required public behavior sections within UTF-8 budgets."""
    sections = context.agent_profile_sections
    if set(sections) != set(_BEHAVIOR_SPEC_FIELDS):
        raise ValidationError(
            "Official Case generation requires the three parsed behavior sections",
            code="agent_profile_invalid",
        )
    behavior_spec: dict[str, str] = {}
    for name in _BEHAVIOR_SPEC_FIELDS:
        value = sections.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                f"Agent Profile behavior section is invalid: {name}",
                code="agent_profile_invalid",
            )
        normalized = value.strip()
        if len(normalized) > _MAX_BEHAVIOR_FIELD_CHARS:
            raise LimitExceededError(
                f"Agent Profile behavior section exceeds "
                f"{_MAX_BEHAVIOR_FIELD_CHARS} Unicode characters: {name}",
                code="behavior_spec_too_large",
            )
        try:
            encoded = normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationError(
                f"Agent Profile behavior section is not valid UTF-8: {name}",
                code="agent_profile_invalid",
            ) from exc
        if len(encoded) > _MAX_BEHAVIOR_FIELD_BYTES:
            raise LimitExceededError(
                f"Agent Profile behavior section exceeds {_MAX_BEHAVIOR_FIELD_BYTES} "
                f"UTF-8 bytes: {name}",
                code="behavior_spec_too_large",
            )
        behavior_spec[name] = normalized
    encoded_spec = json.dumps(
        behavior_spec,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded_spec) > _MAX_BEHAVIOR_SPEC_BYTES:
        raise LimitExceededError(
            f"behavior_spec exceeds {_MAX_BEHAVIOR_SPEC_BYTES} UTF-8 bytes",
            code="behavior_spec_too_large",
        )
    return behavior_spec


def _safe_case_payload(
    context: CaseGenerationContext,
    *,
    allow_sensitive: bool,
    evidence_capabilities: tuple[str, ...],
    max_steps: int | None,
) -> tuple[dict[str, Any], str]:
    """Build and privacy-scan the exact public Case-generation request payload.

    ``max_steps`` remains the caller's requested upper bound on the public wire.
    A separate entitlement preflight rejects unsupported values before this
    payload can start a paid operation; Backend validation remains authoritative.

    Args:
        context: Validated Agent Profile, repository metadata, strategy, and local
            Case normalization ceiling for this Run.
        allow_sensitive: Whether ordinary allowlisted metadata may pass the
            scanner. Secrets and private fields remain forbidden.
        evidence_capabilities: Canonically ordered runtime Evidence kinds that
            this Run can truthfully produce.
        max_steps: Explicit user ceiling to serialize, or ``None`` to omit the
            field and select the service default.

    Returns:
        Detached public JSON payload and the canonical repository fingerprint
        used later to validate the returned Case.

    Raises:
        ConfigurationError: If official generation cannot represent the Input.
        ValidationError: If required behavior sections are malformed.
        LimitExceededError: If public text or metadata exceeds a size bound.
        SensitiveDataError: If the upload scanner rejects the public fields.

    Postconditions:
        The payload contains no raw Agent Profile body, repository contents,
        private rubric, service key, or model configuration.

    Side Effects:
        None. This helper does not perform entitlement or Case network requests.
    """
    if context.input_type != "text":
        raise ConfigurationError(
            "The official Case service currently supports text Inputs only"
        )
    repo_meta = prepare_repo_meta_upload(context.repo_meta)
    safe_fields: dict[str, Any] = {
        "repo_meta": repo_meta,
        "agent_description": _agent_description(context),
        "behavior_spec": _behavior_spec(context),
    }
    if context.strategy_group_selection is not None:
        safe_fields["strategy_group_selection"] = (
            validate_strategy_group_wire_selection(context.strategy_group_selection)
        )
    findings = scan_sensitive_json(safe_fields, location="case_generation_request")
    enforce_sensitive_policy(findings, allow_sensitive=allow_sensitive)
    payload: dict[str, Any] = {
        "strategy_id": context.strategy,
        "count": 1,
        **safe_fields,
    }
    # Omission and explicit 10 are the same frozen public request. Keeping both
    # off the wire preserves the legacy request identity and idempotency key.
    if max_steps is not None and max_steps != DEFAULT_CASE_MAX_STEPS:
        payload["max_steps"] = max_steps
    if evidence_capabilities:
        payload["evidence_capabilities"] = list(evidence_capabilities)
    return payload, repo_meta["repo_fingerprint"]


def _case_operation_store(
    context: CaseGenerationContext,
    *,
    payload: Mapping[str, Any],
    base_url: str,
    api_key_sha256: str,
) -> PendingOperationStore | RequestOperationStore:
    """Bind one Case request to the addressable repository request ledger."""
    if not isinstance(context, CaseGenerationContext):
        raise ProviderError("Case context is invalid", code="request_state_invalid")
    if not context.repo_path.is_dir():
        return PendingOperationStore(
            None,
            operation_type="case_generation",
            base_url=base_url,
        )
    return RequestOperationStore(
        context.repo_path,
        request_type="case_generation",
        request_sha256=canonical_request_sha256(payload),
        base_url=base_url,
        api_key_sha256=api_key_sha256,
        case_validation={
            "repo_fingerprint": payload["repo_meta"]["repo_fingerprint"],
            "max_steps": context.max_steps,
            "strategy_id": context.strategy,
            "strategy_group_selection": (
                None
                if context.strategy_group_selection is None
                else validate_strategy_group_wire_selection(
                    context.strategy_group_selection
                )
            ),
        },
    )


def _normalized_case(
    response: Mapping[str, Any],
    *,
    repo_fingerprint: str,
    max_steps: int,
    requested_strategy_id: str,
    requested_strategy_group: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Convert a bounded Backend result to public Case data with opaque provenance.

    Args:
        response: Terminal operation result returned by the public Backend.
        repo_fingerprint: Digest sent in the Case request and required in every
            returned Case.
        max_steps: Effective public step ceiling. Omitted and explicit-default
            requests both use 10, rather than the legacy internal ceiling of 50.
        requested_strategy_id: Legacy/member strategy request used only when no
            Strategy Group was resolved.
        requested_strategy_group: Exact resolved Group coordinate, which remains
            distinct from the returned member strategy identity.

    Returns:
        Normalized text Case mapping with validated, opaque official provenance.

    Raises:
        ProviderError: If the response exceeds ``max_steps`` or violates the
            closed public Case/integrity contract.

    Postconditions:
        The result contains the complete Case; it is never truncated to fit the
        requested upper bound. Official provenance records the actual returned
        member strategy; a requested Strategy Group remains a separate identity.
    """
    requested_strategy_version = None
    (
        case_id,
        batch_id,
        strategy_id,
        strategy_version,
        raw_case,
        inputs,
    ) = _official_case_response(
        response,
        max_steps=max_steps,
        requested_strategy_id=requested_strategy_id,
        requested_strategy_version=requested_strategy_version,
        requested_strategy_group=requested_strategy_group,
        repo_fingerprint=repo_fingerprint,
    )
    provenance = validate_official_case_provenance(
        {
            "batch_id": batch_id,
            "case_sha256": canonical_sha256(raw_case),
            "case_signature": raw_case["signature"],
            "repo_fingerprint": raw_case["repo_fingerprint"],
            "schema_version": raw_case.get("schema_version"),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
        }
    )
    return {
        "case_id": case_id,
        "inputs": inputs,
        "input_type": "text",
        "extensions": {"official_case": provenance},
    }


class OfficialCaseProvider:
    """Generate and validate public text Cases through the Website Backend.

    The provider submits an idempotent asynchronous v2 operation, polls its
    opaque operation ID, validates the complete public result and provenance,
    and only then clears resumable metadata. It never calls Core MCP, a model,
    or a database directly.
    """

    agent_profile_required = True

    def __init__(
        self,
        client: BackendClient,
        *,
        allow_sensitive: bool = False,
        operation_wait_timeout: float = 600.0,
        max_steps: int | None = None,
    ) -> None:
        """Configure public Case transport, privacy policy, and wait deadline.

        Args:
            client: Authenticated public Backend client.
            allow_sensitive: Whether ordinary Agent Profile metadata accepted by
                the scanner may be transmitted. Private fields/secrets remain
                forbidden regardless of this value.
            operation_wait_timeout: Total positive seconds allowed for POST plus
                all polls of one accepted operation.
            max_steps: Optional caller-requested Case step ceiling sent to the
                Backend. When supplied directly, it must equal the later
                ``CaseGenerationContext.max_steps``. Values above the advertised
                service ceiling fail before operation creation; ``None`` lets
                the context select a non-default ceiling or the default of 10.

        Preconditions:
            ``client`` targets the public Backend and owns a validated key. If
            ``max_steps`` is supplied, callers must pass the same value in every
            context generated by this provider.

        Postconditions:
            No request has occurred; Evidence capability negotiation starts empty.

        Raises:
            ConfigurationError: If ``max_steps`` is not a positive integer or
                ``None``.
        """
        if max_steps is not None and (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or max_steps < 1
        ):
            raise ConfigurationError("max_steps must be a positive integer")
        self.client = client
        self.allow_sensitive = allow_sensitive
        self.operation_wait_timeout = operation_wait_timeout
        self.max_steps = max_steps
        self._evidence_capabilities: tuple[str, ...] = ()
        self._entitlements: Mapping[str, Any] | None = None

    def _configure_entitlements(self, value: Mapping[str, Any]) -> None:
        """Reuse one entitlement response for capability and limit checks.

        Args:
            value: Public entitlement mapping already fetched by ``create_run``.

        Postconditions:
            A later fresh Case request validates ``max_steps`` against this exact
            response instead of issuing a duplicate GET. Pending-operation resume
            still skips the preflight entirely.

        Side Effects:
            Replaces only this provider's in-memory entitlement snapshot; no
            filesystem or network operation occurs.
        """
        self._entitlements = value

    def _configure_evidence_capabilities(self, values: tuple[str, ...]) -> None:
        """Store canonical capabilities only when claim plus observable facts are available."""
        declared = set(values)
        ordered = tuple(
            item for item in CASEGEN_EVIDENCE_CAPABILITY_ORDER if item in declared
        )
        observable = {"file_change", "artifact_snapshot"}
        if (
            ordered != values
            or declared - observable - {"agent_response_claim"}
            or (declared and "agent_response_claim" not in declared)
            or (declared and not observable.intersection(declared))
        ):
            raise ConfigurationError("Runtime Evidence capabilities are invalid")
        self._evidence_capabilities = values

    def _max_steps_request(
        self, context: CaseGenerationContext
    ) -> tuple[int | None, int | None]:
        """Resolve wire omission and preflight without weakening the context cap.

        Args:
            context: Public Provider request whose ``max_steps`` is the
                authoritative result upper bound.

        Returns:
            ``(wire_value, preflight_value)``. The wire value is omitted for the
            frozen default of 10 so omission and explicit 10 share identity. A
            preflight value is present whenever the caller explicitly configured
            this provider or the context requests a non-default ceiling.

        Raises:
            ConfigurationError: If the constructor's explicit value differs from
                ``context.max_steps``. Rejection occurs before network or local
                pending-state effects.

        Postconditions:
            The returned values can control transport behavior, but result
            validation continues to use ``context.max_steps`` unchanged.

        Side Effects:
            None.
        """
        if self.max_steps is not None and self.max_steps != context.max_steps:
            raise ConfigurationError(
                "OfficialCaseProvider max_steps must match context.max_steps"
            )
        wire_value = (
            None if context.max_steps == DEFAULT_CASE_MAX_STEPS else context.max_steps
        )
        preflight_value = (
            context.max_steps
            if self.max_steps is not None or wire_value is not None
            else None
        )
        return wire_value, preflight_value

    def generate_case(self, context: CaseGenerationContext) -> Mapping[str, Any]:
        """Generate or resume one official Case and return validated public data.

        Args:
            context: Immutable Agent Profile, repository metadata, strategy, and
                maximum-step inputs prepared by ``create_run``.

        Returns:
            Mapping accepted by ``normalize_case`` with public inputs and opaque
            validated official provenance. Private rubric content is absent.

        Raises:
            SensitiveDataError: If uploadable public inputs violate privacy policy.
            ProviderError: If operation wire, Case integrity, strategy selection,
                or public result is invalid.
            KumaError: For authenticated transport, limits, service failure, or
                bounded wait timeout.

        Preconditions:
            Agent Profile is present and ``context.max_steps`` is positive.

        Postconditions:
            Success has one through ``max_steps`` complete inputs. A terminal
            validated result clears pending state; timeout/transport interruption
            retains it so retry resumes the same operation and key.

        Side Effects:
            May persist non-secret pending metadata and perform public Backend
            POST/GET requests. It never truncates a valid Case.

        Security/Privacy:
            Only allowlisted public Agent Profile/repository metadata is sent. No
            private rubric, hidden answer, provider key, MCP address, or model
            configuration enters the SDK result.
        """

        wire_max_steps, preflight_max_steps = self._max_steps_request(context)
        payload, repo_fingerprint = _safe_case_payload(
            context,
            allow_sensitive=self.allow_sensitive,
            evidence_capabilities=self._evidence_capabilities,
            max_steps=wire_max_steps,
        )
        return self._run_operation(
            context,
            payload,
            repo_fingerprint=repo_fingerprint,
            preflight_max_steps=preflight_max_steps,
        )

    def _run_operation(
        self,
        context: CaseGenerationContext,
        payload: Mapping[str, Any],
        *,
        repo_fingerprint: str,
        preflight_max_steps: int | None,
    ) -> Mapping[str, Any]:
        """Run or resume v2 Case generation and validate before clearing state.

        Args:
            context: Current Run's immutable Case-generation correlation data.
            payload: Privacy-checked public request whose canonical bytes define
                pending-state identity and Backend idempotency semantics.
            repo_fingerprint: Digest that the returned public Case must repeat.
            preflight_max_steps: Explicit/non-default ceiling to validate against
                entitlements before a fresh POST, or ``None`` for service-default
                omission.

        Returns:
            Normalized official Case mapping accepted by ``normalize_case``.

        Raises:
            LimitExceededError: A new explicit request exceeds the entitlement
                ceiling; no Case POST has occurred.
            ProviderError: Entitlements, operation envelopes, or Case result are
                malformed or fail integrity validation.
            KumaError: Public transport, service, or bounded-wait failure.

        Preconditions:
            ``payload`` was produced by ``_safe_case_payload`` for ``context``.

        Postconditions:
            Success clears pending metadata only after Case acceptance. Existing
            pending state bypasses the entitlement preflight and resumes the same
            operation/key, so later service-limit changes cannot cause a repost.

        Side Effects:
            May GET entitlements, atomically write/delete safe pending metadata,
            POST one idempotent Case operation, poll it, and sleep within the
            configured deadline.

        Security/Privacy:
            Pending files contain only operation metadata; neither request
            content nor credentials are persisted.
        """
        store = _case_operation_store(
            context,
            payload=payload,
            base_url=self.client.base_url,
            api_key_sha256=_client_credential_identity(self.client),
        )

        if preflight_max_steps is not None and store.load() is None:
            entitlements = self._entitlements
            if entitlements is None:
                entitlements = self.client.json("GET", "/sdk/entitlements/")
            _validate_requested_max_steps(entitlements, preflight_max_steps)

        def start_operation(key: str, deadline: float) -> Mapping[str, Any]:
            """POST the Case payload once per stable key within the shared deadline."""
            kwargs: dict[str, Any] = {"idempotency_key": key}
            if isinstance(self.client, BackendClient) and isinstance(
                store, RequestOperationStore
            ):
                kwargs["client_request_id"] = store.client_request_id
            if isinstance(self.client, BackendClient):
                kwargs["_deadline"] = deadline
                kwargs["_expected_status"] = 202
            return self.client.json(
                "POST",
                "/sdk/v2/cases/generate/",
                payload,
                **kwargs,
            )

        def accept_result(response: Mapping[str, Any]) -> Mapping[str, Any]:
            """Validate and normalize success before resumable state may be cleared."""
            accepted = _normalized_case(
                response,
                repo_fingerprint=repo_fingerprint,
                max_steps=context.max_steps,
                requested_strategy_id=context.strategy,
                requested_strategy_group=context.strategy_group_selection,
            )
            if isinstance(store, RequestOperationStore):
                store.stage_public_result(case_id=str(accepted["case_id"]))
            return accepted

        return await_operation(
            self.client,
            store,
            key_factory=lambda: new_idempotency_key("casegen"),
            start=start_operation,
            wait_timeout=self.operation_wait_timeout,
            accept_result=accept_result,
        )


__all__ = ["OfficialCaseProvider"]
