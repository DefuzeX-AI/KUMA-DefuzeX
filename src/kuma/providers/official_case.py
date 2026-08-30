"""Official Case Provider for the Website-backend public API."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..errors import (
    ConfigurationError,
    LimitExceededError,
    ProviderError,
    ValidationError,
)
from ..evidence.runtime_contract import CASEGEN_EVIDENCE_CAPABILITY_ORDER
from ..repository.metadata import prepare_repo_meta_upload
from ..repository.privacy import enforce_sensitive_policy, scan_sensitive_json
from ..transport.backend import BackendClient, new_idempotency_key
from ..transport.operations import (
    PendingOperationStore,
    await_operation,
    request_identity,
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


def _official_inputs(steps: Any, *, max_inputs: int) -> list[dict[str, str]]:
    if not isinstance(steps, list) or not 1 <= len(steps) <= max_inputs:
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
    max_inputs: int,
    requested_strategy_id: str,
    repo_fingerprint: str,
) -> tuple[str, str, str, str, Mapping[str, Any], list[dict[str, str]]]:
    batch, raw_case = _case_response_envelope(response)
    case_id = required_text(raw_case.get("case_id"), "case_id")
    batch_id = required_text(batch.get("batch_id"), "batch_id")
    strategy_id, strategy_version = _selected_strategy(
        batch,
        requested_strategy_id=requested_strategy_id,
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
        _official_inputs(raw_case.get("steps"), max_inputs=max_inputs),
    )


def _case_response_envelope(
    response: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
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
    batch: Mapping[str, Any], *, requested_strategy_id: str
) -> tuple[str, str]:
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
    if (
        requested_strategy_id != "auto"
        and selected_strategy_id != requested_strategy_id
    ):
        raise ProviderError(
            "The Backend Case strategy does not match the explicit request",
            code="invalid_response",
        )
    return selected_strategy_id, selected_strategy_version


def _agent_description(context: CaseGenerationContext) -> str:
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
    sections = context.requirement_sections
    if set(sections) != set(_BEHAVIOR_SPEC_FIELDS):
        raise ValidationError(
            "Official Case generation requires the three parsed behavior sections",
            code="requirement_invalid",
        )
    behavior_spec: dict[str, str] = {}
    for name in _BEHAVIOR_SPEC_FIELDS:
        value = sections.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                f"Requirement behavior section is invalid: {name}",
                code="requirement_invalid",
            )
        normalized = value.strip()
        if len(normalized) > _MAX_BEHAVIOR_FIELD_CHARS:
            raise LimitExceededError(
                f"Requirement behavior section exceeds "
                f"{_MAX_BEHAVIOR_FIELD_CHARS} Unicode characters: {name}",
                code="behavior_spec_too_large",
            )
        try:
            encoded = normalized.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationError(
                f"Requirement behavior section is not valid UTF-8: {name}",
                code="requirement_invalid",
            ) from exc
        if len(encoded) > _MAX_BEHAVIOR_FIELD_BYTES:
            raise LimitExceededError(
                f"Requirement behavior section exceeds {_MAX_BEHAVIOR_FIELD_BYTES} "
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
) -> tuple[dict[str, Any], str]:
    if context.input_type != "text":
        raise ConfigurationError(
            "The official Case service currently supports text Inputs only"
        )
    repo_meta = prepare_repo_meta_upload(context.repo_meta)
    safe_fields = {
        "repo_meta": repo_meta,
        "agent_description": _agent_description(context),
        "behavior_spec": _behavior_spec(context),
    }
    findings = scan_sensitive_json(safe_fields, location="case_generation_request")
    enforce_sensitive_policy(findings, allow_sensitive=allow_sensitive)
    payload: dict[str, Any] = {
        "strategy_id": context.strategy,
        "count": 1,
        **safe_fields,
    }
    if evidence_capabilities:
        payload["evidence_capabilities"] = list(evidence_capabilities)
    return payload, repo_meta["repo_fingerprint"]


def _case_operation_store(
    context: CaseGenerationContext,
    *,
    payload: Mapping[str, Any],
    base_url: str,
) -> PendingOperationStore:
    identity = request_identity(payload, base_url=base_url)
    return PendingOperationStore(
        context.repo_path / ".kuma" / "operations" / "cases" / f"{identity}.json",
        operation_type="case_generation",
        base_url=base_url,
    )


def _normalized_case(
    response: Mapping[str, Any],
    *,
    context: CaseGenerationContext,
    repo_fingerprint: str,
) -> Mapping[str, Any]:
    case_id, batch_id, strategy_id, strategy_version, raw_case, inputs = (
        _official_case_response(
            response,
            max_inputs=context.max_inputs,
            requested_strategy_id=context.strategy,
            repo_fingerprint=repo_fingerprint,
        )
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
    """Generate and validate text Cases through the public Website Backend."""

    requirement_required = True

    def __init__(
        self,
        client: BackendClient,
        *,
        allow_sensitive: bool = False,
        operation_wait_timeout: float = 600.0,
    ) -> None:
        self.client = client
        self.allow_sensitive = allow_sensitive
        self.operation_wait_timeout = operation_wait_timeout
        self._evidence_capabilities: tuple[str, ...] = ()

    def _configure_evidence_capabilities(self, values: tuple[str, ...]) -> None:
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

    def generate_case(self, context: CaseGenerationContext) -> Mapping[str, Any]:
        """Return a public Case shape after strategy and integrity validation."""

        payload, repo_fingerprint = _safe_case_payload(
            context,
            allow_sensitive=self.allow_sensitive,
            evidence_capabilities=self._evidence_capabilities,
        )
        return self._run_operation(
            context,
            payload,
            repo_fingerprint=repo_fingerprint,
        )

    def _run_operation(
        self,
        context: CaseGenerationContext,
        payload: Mapping[str, Any],
        *,
        repo_fingerprint: str,
    ) -> Mapping[str, Any]:
        store = _case_operation_store(
            context,
            payload=payload,
            base_url=self.client.base_url,
        )

        def start_operation(key: str, deadline: float) -> Mapping[str, Any]:
            kwargs: dict[str, Any] = {"idempotency_key": key}
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
            return _normalized_case(
                response,
                context=context,
                repo_fingerprint=repo_fingerprint,
            )

        return await_operation(
            self.client,
            store,
            key_factory=lambda: new_idempotency_key("casegen"),
            start=start_operation,
            wait_timeout=self.operation_wait_timeout,
            accept_result=accept_result,
        )


__all__ = ["OfficialCaseProvider"]
