"""Authenticated trajectory workflows through KUMA; no separate storage or model credentials in clients."""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any

from .errors import AuthenticationError, KumaError, ProviderError, ValidationError
from .evidence.trace_tool_content import normalize_tool_content
from .trajectory_capture import prepare_submission
from .trajectory_requirements import validate_requirements
from .trajectory_wire import export_digest, validate_export_scope, validate_response
from .transport.backend import _validate_timeout

READ_ACTIONS = frozenset(
    {
        "requirements.get",
        "requirements.list",
        "traces.get",
        "traces.list",
        "reviews.get",
        "reviews.list",
        "datasets.get",
        "datasets.list",
        "experiments.get",
        "experiments.list",
        "experiments.compare",
        "feedback.get",
        "feedback.list",
        "requirements.export",
        "traces.export",
        "reviews.export",
        "datasets.export",
        "experiments.export",
        "feedback.export",
    }
)
WRITE_ACTIONS = frozenset(
    {
        "requirements.save",
        "traces.import",
        "datasets.create",
        "experiments.create",
        "feedback.submit",
    }
)


def _safe(value: Any, *, remote: bool = False) -> dict[str, Any]:
    """Reject unsafe/malformed graphs without returning raw local or remote values."""
    if remote:
        return _remote_safe(value)
    result, status = normalize_tool_content(value, redact=True)
    if status != "present" or not isinstance(result, dict):
        raise ValidationError(
            "Trajectory data is invalid or requires privacy correction",
            code="trajectory_invalid",
        )
    return result


def _remote_safe(value: Any) -> dict[str, Any]:
    """Admit an eight-MiB finite response with existing depth/privacy primitives, without rewriting it."""
    from .repository.privacy import scan_sensitive_json
    from .repository.tool_capabilities import _plain_json

    failed, result = False, None
    try:
        result = _plain_json(dict(value) if isinstance(value, dict) else value)
        if (
            not isinstance(result, dict)
            or len(
                json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
            > 8 * 1024 * 1024
            or scan_sensitive_json(result, location="trajectory_response")
        ):
            raise ValueError("Invalid trajectory response")
    except Exception:
        failed = True
    if failed:
        raise ProviderError(
            "Invalid trajectory workspace response", code="invalid_response"
        )
    return result


def _review_input(value: dict[str, Any]) -> None:
    """Reject malformed public paid-review identifiers and budgets before HTTP admission."""
    bounds = {
        "decisions": (1, 12),
        "tool_calls": (1, 24),
        "evidence_characters": (1000, 120000),
        "seconds": (1, 900),
        "source_characters": (1000, 4194304),
        "chunk_characters": (1000, 350000),
    }
    identity, limits = value.get("trace_id"), value.get("limits", {})
    if (
        set(value) - {"trace_id", "limits"}
        or not isinstance(identity, str)
        or re.fullmatch(r"[0-9a-f]{64}\.traces\.[0-9a-f]{64}", identity) is None
        or not isinstance(limits, dict)
        or set(limits) - set(bounds)
    ):
        raise ValidationError("Invalid trajectory review input")
    for key, number in limits.items():
        lower, upper = bounds[key]
        if type(number) is not int or not lower <= number <= upper:
            raise ValidationError("Invalid trajectory review budget")


class TrajectoryClient:
    """Manage the complete trajectory workflow using an existing KumaClient credential.

    All writes/submissions are explicit. observe() and export preparation stay local. Model
    review requires judge:run; reads sdk:read; catalog/import/feedback changes sdk:observe.
    """

    def __init__(self, client: Any, *, timeout: float = 180):
        """Reuse existing KUMA configuration without reading a new key or changing parent timeouts.

        Args:
            client: Existing kuma.KumaClient, normally produced by client.trajectory().
            timeout: Positive finite per-request seconds, default 180 for bounded process reviews.
        Raises:
            ConfigurationError: Invalid timeout, before any HTTP request.
        Returns:
            TrajectoryClient sharing the existing credential through a copied transport.
        Side Effects:
            None; a shallow private transport copy disables automatic retries for this capability.
            No model client, tracing platform, credentials file, database or collector is added.
        """
        self._backend = copy.copy(client._backend)
        self.timeout = _validate_timeout(timeout)
        if self._backend is not None:
            self._backend.timeout = self.timeout
            self._backend.max_retries = 0

    def request(
        self,
        action: str,
        data: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        accept_media_omission: bool = False,
    ) -> dict[str, Any]:
        """Execute one allowlisted workspace command using normal KUMA authentication.

        Args:
            action: Exact supported workspace action; see docs/trajectory-platform.md.
            data: Action-specific finite JSON fields; no owner, provider URL or credentials.
            idempotency_key: Required for reviews.run, 8..200 ASCII letters/digits/._:-.
                Reuse unchanged to recover the same logical paid review; never reuse with new input.
            accept_media_omission: Strict bool, default False for legacy raw requests.
                True permits text-and-structure review and scoped artifact reads; media
                is not understood. Changing this option with a manual key conflicts.
        Returns:
            Detached user-owned result from kuma.trajectory_workspace.v1, with no automatic pagination.
        Raises:
            ValidationError: Unsupported action, invalid key or unsafe input before HTTP.
            ProviderError: Malformed/private/sensitive remote response.
            KumaError: Auth/scope, credits, conflicts, feature, timeout or platform failure.
        Side Effects:
            One authenticated public Backend POST, no retries. reviews.run may consume the existing
            Judge fee only after completion; imports, storage, labels and reads never invoke Judge.
            A timeout is an unknown server outcome; retry explicitly with the same key, not a new key.
        """
        if not isinstance(action, str) or action not in READ_ACTIONS | WRITE_ACTIONS | {
            "reviews.run"
        }:
            raise ValidationError("Unsupported trajectory workspace action")
        if type(accept_media_omission) is not bool:
            raise ValidationError("accept_media_omission must be a boolean")
        safe = _safe(data)
        if action == "reviews.run":
            _review_input(safe)
        if (action == "reviews.run" or idempotency_key is not None) and (
            not isinstance(idempotency_key, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", idempotency_key)
            is None
        ):
            raise ValidationError("A valid trajectory idempotency key is required")
        if self._backend is None:
            raise AuthenticationError("Set KUMA_API_KEY or pass api_key to KumaClient.")
        mode = (
            "read"
            if action in READ_ACTIONS
            else "write"
            if action in WRITE_ACTIONS
            else "review"
        )
        command = {"action": action, "data": safe}
        if accept_media_omission:
            command["accept_media_omission"] = True
        response = self._backend.json(
            "POST",
            f"/sdk/trajectory/{mode}/",
            command,
            idempotency_key=idempotency_key or "trajectory-" + uuid.uuid4().hex,
            _expected_status=200,
        )
        response = _safe(response, remote=True)
        return validate_response(
            response, action, safe, accept_media_omission=accept_media_omission
        )

    def save_requirements(self, requirements: Mapping[str, Any]) -> dict[str, Any]:
        """Explicitly save one caller requirements revision.

        Args:
            requirements: Complete v1 requirements mapping; validated locally before transport.
        Returns:
            Owned immutable requirements receipt with ID, hash and display summary.
        Raises:
            ValidationError: Invalid or sensitive profile before HTTP; request errors also apply.
        Side Effects:
            One authenticated write, without model calls or Judge charges. Content is immutable.
        """
        return self.request(
            "requirements.save", {"requirements": validate_requirements(requirements)}
        )

    def export(
        self, kind: str, identity: str, *, accept_media_omission: bool = True
    ) -> bytes:
        """Retrieve an exact canonical JSON artifact without changing large integers or source types.

        Args:
            kind: requirements, traces, reviews, datasets, experiments or feedback.
            identity: Owned document ID returned by the workspace.
            accept_media_omission: Strict bool, default True to read scoped artifacts.
                False retains legacy request semantics and rejects media-scoped reports.
        Returns:
            UTF-8 JSON bytes bound to the requested ID digest, at most 6 MiB uncompressed.
            Response identity, category, declared digest and decoded-byte hash must agree.
            The caller owns saving the bytes.
        Raises:
            ValidationError: Unsupported category or malformed/category-mismatched ID;
                rejected before HTTP. Standard request errors also apply.
            ProviderError: Bad encoding, expansion limit or mismatched content hash.
        Side Effects:
            One read-only POST; no local file, model, credit or automatic retry.
        """
        if not isinstance(kind, str) or kind + ".export" not in READ_ACTIONS:
            raise ValidationError("Invalid trajectory export category")
        try:
            expected_digest = export_digest(identity, kind)
        except ValueError:
            raise ValidationError("Invalid trajectory export identity") from None
        result = self.request(
            kind + ".export",
            {"id": identity},
            accept_media_omission=accept_media_omission,
        )
        failed, raw = False, b""
        try:
            import io

            with gzip.GzipFile(
                fileobj=io.BytesIO(base64.b64decode(result["content"], validate=True))
            ) as stream:
                raw = stream.read(6 * 1024 * 1024 + 1)
            if (
                len(raw) > 6 * 1024 * 1024
                or result["sha256"] != expected_digest
                or hashlib.sha256(raw).hexdigest() != result["sha256"]
            ):
                raise ValueError("Invalid artifact size or hash")
        except (ValueError, TypeError, KeyError, OSError, EOFError):
            failed = True
        if failed:
            raise ProviderError("Invalid trajectory export", code="invalid_response")
        validate_export_scope(raw, kind, accept_media_omission=accept_media_omission)
        return raw

    def upload(
        self,
        submission: Mapping[str, Any],
        *,
        agent_version: str | None = None,
        case_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Explicitly import a prepared submission with declared version/case/tags; never evaluates it.

        Args:
            submission: v1 prepare_submission/prepare_observation output, revalidated before HTTP.
            agent_version: Optional caller build/model label up to 80 characters, not verified identity.
            case_id: Optional stable caller test-case ID up to 128 characters; required for version matching.
            tags: Optional list of up to 32 labels of 1..120 characters, default empty.
        Returns:
            Owned trace document receipt; unchanged content reuses its content identity.
        Raises:
            ValidationError: Invalid/mismatched input before HTTP; standard request errors also apply.
        Side Effects:
            One explicit Backend write; no model, credits or file changes.
        """
        value = _safe(submission)
        if (
            set(value)
            != {
                "schema_version",
                "bundle",
                "requirements",
                "requirements_sha256",
                "observation_id",
                "inventory",
            }
            or value["schema_version"] != "kuma.trajectory_submission.v1"
        ):
            raise ValidationError("Invalid trajectory submission")
        safe = prepare_submission(
            value["bundle"],
            value["requirements"],
            observation_id=value["observation_id"],
            inventory=value["inventory"],
        )
        if safe != value:
            raise ValidationError(
                "Trajectory requirements revision does not match content"
            )
        for text, size in ((agent_version, 80), (case_id, 128)):
            if text is not None and (
                not isinstance(text, str) or not text.strip() or len(text) > size
            ):
                raise ValidationError("Invalid trajectory metadata")
        labels = [] if tags is None else tags
        if (
            not isinstance(labels, list)
            or len(labels) > 32
            or any(
                not isinstance(t, str) or not t.strip() or len(t) > 120 for t in labels
            )
        ):
            raise ValidationError("Invalid trajectory tags")
        return self.request(
            "traces.import",
            {
                "submission": safe,
                "agent_version": agent_version,
                "case_id": case_id,
                "tags": labels,
            },
        )

    def upload_atif(
        self,
        document: Mapping[str, Any],
        requirements: Mapping[str, Any],
        *,
        agent_version: str | None = None,
        case_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Upload existing ATIF without collecting or executing an Agent.

        Args:
            document: ATIF 1.0..1.8 source JSON; original recorded metadata is preserved.
                Versions before 1.7 require a recorded string session_id; references
                remain inert and are never upgraded or resolved.
                Optional root IDs and 1.7/1.8 sessions retain absence/null. Missing root IDs or
                cross-scope repeated IDs use explicitly marked derived coordinates.
            requirements: Actual reviewed v1 requirements; unknown tools stay null.
            agent_version: Optional caller build label, at most 80 characters.
            case_id: Optional stable task identity, at most 128 characters.
            tags: Optional list of at most 32 metadata labels, each 1..120 characters.
        Returns:
            Owned trace receipt from upload(); no model review is automatically requested.
        Raises:
            ValidationError: Unsafe/invalid/oversized source or declarations before HTTP.
            KumaError: Existing authentication, scope, transport or storage errors.
        Side Effects:
            One explicit Backend write using the existing Kuma key. No file/URL resolution,
            model calls or Judge credit consumption; the caller's source object is unchanged.
        """
        from .trajectory_atif import prepare_atif

        profile = validate_requirements(requirements)
        bundle = prepare_atif(
            profile["objective"],
            document,
            sample=profile["name"],
            tools=profile["tools"],
            limitations=profile["limitations"],
        )
        return self.upload(
            prepare_submission(bundle, profile),
            agent_version=agent_version,
            case_id=case_id,
            tags=tags,
        )

    def evaluate(
        self,
        trace_id: str,
        *,
        idempotency_key: str,
        limits: Mapping[str, Any] | None = None,
        accept_media_omission: bool = True,
    ) -> dict[str, Any]:
        """Run the private process Judge on one owned stored trace, without running the Agent.

        Args:
            trace_id: Owned document ID returned by upload/list, not a model provider trace URL.
            idempotency_key: Required unchanged logical review identity; see request() for constraints.
            limits: Optional integer budgets: decisions 1..12 (default 6), tool_calls 1..24
                (12), evidence_characters 1000..120000 (40000), seconds 1..900 (120),
                source_characters 1000..4194304 (4194304), chunk_characters 1000..350000
                (240000). None leaves defaults to the service; deployment lease/timeout
                constraints may reduce accepted seconds. See docs/trajectory-platform.md.
            accept_media_omission: Strict bool, default True. Permit bounded text-and-structure
                review while preserving the original trace and marking unreviewed media.
                False keeps legacy media rejection. Reuse the original value on recovery.
        Returns:
            Completed report with blank human labels, or an explicit stopped artifact with reason.
        Raises:
            ValidationError: Invalid trace ID, key, budget or sensitive input before HTTP.
            ProviderError: Malformed or unsafe remote response.
            KumaError: Missing authentication, scope, credit, timeout or service failure;
                a timeout is an unknown outcome, not evidence that no review occurred.
        Side Effects:
            One explicit billable request; completed review uses the configured Judge fixed fee.
            No hidden retries, gold-label generation or automatic score penalties for retries/gaps.
        """
        data: dict[str, Any] = {"trace_id": trace_id}
        if limits is not None:
            data["limits"] = dict(limits)
        return self.request(
            "reviews.run",
            data,
            idempotency_key=idempotency_key,
            accept_media_omission=accept_media_omission,
        )

    def run_experiment(
        self,
        name: str,
        dataset_id: str,
        *,
        idempotency_key: str,
        limits: Mapping[str, Any] | None = None,
        accept_media_omission: bool = True,
    ) -> dict[str, Any]:
        """Review each frozen dataset trace sequentially and preserve failed/missing cases.

        Args:
            name: Nonblank experiment label up to 200 characters, frozen by the server.
            dataset_id: Owned dataset document with 1..50 ordered trace IDs.
            idempotency_key: Required run identity; deterministic child keys recover completed
                trace reviews on explicit rerun. Changing name, dataset or limits changes children.
            limits: Optional per-trace budgets, forwarded unchanged to evaluate.
            accept_media_omission: Strict bool, default True for text-and-structure review.
                False retains legacy child keys. True binds the mode into generated child
                keys; changing the mode requests separate reviews, not automatic recovery.
        Returns:
            Always a dict with exactly experiment, reviews and errors on normal return.
            experiment is a saved experiment receipt (dict), or None only when no review
            artifact was returned. reviews is an ordered list of validated completed/stopped
            review dicts. errors is an ordered list of {trace_id: str, code: str} for
            individual evaluate() KumaError failures; it contains no exception messages.
            If all evaluations raise KumaError, reviews is empty and experiment is None;
            the outer result is never None. A stopped artifact is retained in reviews,
            not errors, and does not establish successful Agent behavior or complete review.
        Raises:
            ValidationError: Invalid name/key before the dataset read, or unsafe child-key
                seed during iteration; these errors are not converted to per-trace errors.
            ProviderError: Invalid dataset response before evaluations begin.
            KumaError: Initial dataset read or final experiment save fails, with no returned
                wrapper. Only KumaError from individual evaluate() calls is accumulated.
            BaseException: Cancellation/KeyboardInterrupt propagates immediately; completed
                remote work is not rolled back. Unexpected non-KumaError also propagates.
        Preconditions:
            Use an owned frozen dataset and a credential authorized for its traces.
            Explicit recovery must preserve name, dataset, limits and the run key.
        Postconditions:
            Normal return preserves each returned artifact or per-trace error in input
            order. A raised read/save/cancellation error does not roll back remote work.
        Security/Privacy:
            Uses the existing authenticated trajectory boundary; errors contain safe
            categories, not exception text. Returned reviews remain caller-owned data.
        Side Effects:
            One dataset read, up to 50 explicit review requests and, only if reviews exist,
            one experiment write. No experiment is saved when every evaluation raises.
            Each newly completed review can charge its configured Judge fee; storage is separate.
            Does not run evaluated Agents, synthesize missing traces or calculate an overall ranking.
        """
        if type(accept_media_omission) is not bool:
            raise ValidationError("accept_media_omission must be a boolean")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValidationError("Invalid experiment name")
        if (
            not isinstance(idempotency_key, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", idempotency_key)
            is None
        ):
            raise ValidationError("A valid experiment idempotency key is required")
        dataset = self.request("datasets.get", {"id": dataset_id})
        trace_ids = dataset.get("payload", {}).get("trace_ids")
        if (
            not isinstance(trace_ids, list)
            or not 1 <= len(trace_ids) <= 50
            or any(not isinstance(t, str) for t in trace_ids)
            or len(set(trace_ids)) != len(trace_ids)
        ):
            raise ProviderError(
                "Invalid trajectory dataset response", code="invalid_response"
            )
        reviews, errors = [], []
        for trace_id in trace_ids:
            seed = _safe(
                {
                    "name": name,
                    "dataset_id": dataset_id,
                    "trace_id": trace_id,
                    "key": idempotency_key,
                    "limits": None if limits is None else dict(limits),
                }
            )
            if accept_media_omission:
                seed["accept_media_omission"] = True
            child = hashlib.sha256(
                json.dumps(seed, sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest()
            try:
                reviews.append(
                    self.evaluate(
                        trace_id,
                        idempotency_key="experiment-" + child,
                        limits=limits,
                        accept_media_omission=accept_media_omission,
                    )
                )
            except KumaError as exc:
                errors.append({"trace_id": trace_id, "code": exc.code})
        experiment = (
            self.request(
                "experiments.create",
                {
                    "name": name,
                    "dataset_id": dataset_id,
                    "review_ids": [r["id"] for r in reviews],
                },
                accept_media_omission=accept_media_omission,
            )
            if reviews
            else None
        )
        return {"experiment": experiment, "reviews": reviews, "errors": errors}
