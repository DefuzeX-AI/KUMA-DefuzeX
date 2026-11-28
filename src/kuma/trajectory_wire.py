"""Public-only trajectory response binding; no private Core model/prompt dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ._json_values import detach_json
from .errors import ProviderError
from .repository.case_artifact_io import _unique_object
from .repository.privacy import scan_sensitive_json
from .trajectory_media import validate_media_assessment, validate_media_coverage


def _require(condition: bool) -> None:
    """Reject malformed public wire values without exposing their contents."""
    if not condition:
        raise ValueError("Invalid trajectory response")


def _receipt(value: dict, kind: str) -> None:
    """Bind content IDs, category and canonical fingerprint while leaving owner authorization server-side."""
    identity = value["id"]
    _require(
        isinstance(identity, str)
        and re.fullmatch(
            r"[0-9a-f]{64}\." + re.escape(kind) + r"\.[0-9a-f]{64}", identity
        )
        is not None
    )
    _require(
        value["kind"] == kind
        and value["content_sha256"] == identity.rsplit(".", 1)[1]
        and isinstance(value["summary"], dict)
    )


def export_digest(identity: Any, kind: str) -> str:
    """Return the digest of a validated immutable export coordinate.

    The client uses this before transport and the response validator reuses it
    before accepting an export. Only the requested category and exact lowercase
    owner/digest format are admitted; ownership authorization remains server-side.
    Invalid input raises ValueError without echoing the coordinate. No I/O occurs.
    """
    _require(
        isinstance(identity, str)
        and re.fullmatch(
            r"[0-9a-f]{64}\." + re.escape(kind) + r"\.[0-9a-f]{64}", identity
        )
        is not None
    )
    return identity.rsplit(".", 1)[1]


def _page(result: dict, kind: str, data: dict) -> None:
    """Validate metadata-only bounded pages, including empty pages with a continuation cursor."""
    _require(
        set(result) == {"items", "next_cursor"} and isinstance(result["items"], list)
    )
    limit = data.get("limit", 20)
    _require(type(limit) is int and len(result["items"]) <= limit)
    owners, identities = set(), set()
    for item in result["items"]:
        _require(
            isinstance(item, dict)
            and set(item) == {"id", "kind", "content_sha256", "summary", "created_at"}
        )
        _receipt(item, kind)
        _require(item["id"] not in identities and isinstance(item["created_at"], str))
        identities.add(item["id"])
        owners.add(item["id"].split(".", 1)[0])
    _require(len(owners) <= 1)
    cursor = result["next_cursor"]
    _require(cursor is None or (isinstance(cursor, str) and 1 <= len(cursor) <= 4096))


def _review(
    result: dict, data: dict, *, accept_media_omission: bool = False
) -> set[str]:
    """Require explicit completion/report agreement; stopped provider work is never a successful score."""
    _require(
        result["trace_id"] == data["trace_id"]
        and result["review_status"] in {"completed", "stopped"}
    )
    _require(
        (result["review_status"] == "completed") == isinstance(result["report"], dict)
    )
    if isinstance(result["report"], dict):
        _require(
            result["report"].get("schema_version")
            in {"kuma.quality_agent_report.v3", "kuma.quality_agent_report.v4"}
        )
    fields = {
        "trace_id",
        "review_status",
        "reason",
        "attempts",
        "report",
        "process_labels",
        "judge_version",
        "requirements_sha256",
        "case_id",
        "agent_version",
    }
    if "coverage" in result:
        _coverage(
            result["coverage"],
            completed=result["review_status"] == "completed",
            accept_media_omission=accept_media_omission,
            attempts=result["attempts"],
            limits={"decisions": 6, **data.get("limits", {})},
        )
        fields.add("coverage")
        if "media_omission" in result["coverage"]:
            _require(result.get("review_scope") == "text_and_structure")
            fields.add("review_scope")
    if (
        isinstance(result["report"], dict)
        and result["report"]["schema_version"] == "kuma.quality_agent_report.v4"
    ):
        _require(
            "coverage" in result
            and "coverage" in result["report"]
            and result["coverage"] == result["report"]["coverage"]
        )
        for segment in result["report"]["segments"]:
            raw = segment["unconfirmed_model_output"]
            _require(
                isinstance(raw, dict)
                and set(raw) == {"status", "process_draft"}
                and raw["status"] == "unconfirmed"
                and isinstance(raw["process_draft"], dict)
            )
    return fields


def _coverage(
    value: dict,
    *,
    completed: bool,
    accept_media_omission: bool = False,
    attempts: list | None = None,
    limits: dict | None = None,
) -> None:
    """Keep physical source coverage explicit and separate from unmeasured semantic recall."""
    fields = {
        "schema_version",
        "sources",
        "total_source_characters",
        "delivered_source_characters",
        "source_delivery_complete",
        "all_segments_accepted",
        "segments",
        "semantic_recall_certified",
        "delivery_is_attention",
    }
    _require(isinstance(value, dict))
    scoped = "media_omission" in value
    if scoped:
        _require(accept_media_omission is True)
        fields.add("media_omission")
    _require(set(value) == fields)
    _require(
        value["schema_version"] == "kuma.process_coverage.v1"
        and value["semantic_recall_certified"] is False
        and value["delivery_is_attention"] is False
    )
    _require(
        isinstance(value["sources"], dict)
        and 1 <= len(value["sources"]) <= 10002
        and isinstance(value["segments"], list)
        and len(value["segments"]) <= 12
    )
    for item in value["sources"].values():
        _require(
            isinstance(item, dict)
            and set(item)
            == {"characters", "delivered_ranges", "uncovered_ranges", "fully_delivered"}
        )
    if scoped:
        validate_media_coverage(
            value, completed=completed, attempts=attempts, limits=limits
        )
    elif completed:
        _require(
            value["source_delivery_complete"] is True
            and value["all_segments_accepted"] is True
            and bool(value["segments"])
        )
        _require(
            all(
                item["fully_delivered"] is True and item["uncovered_ranges"] == []
                for item in value["sources"].values()
            )
        )


def _result(
    result: dict,
    action: str,
    data: dict,
    *,
    accept_media_omission: bool = False,
    redact_sensitive: bool = True,
) -> None:
    """Validate action-specific closed result envelopes before application code sees them.

    Deletion acknowledges only the requested trace ID and a strict Boolean; it
    returns no source body and grants no raw-content read capability.

    Args:
        redact_sensitive: Strict bool, default True. False explicitly retains original
            bounded content for this operation only; existing redaction cannot be undone.
    """
    kind, operation = action.split(".")
    if action == "traces.delete":
        _require(set(result) == {"id", "deleted"})
        export_digest(result["id"], "traces")
        _require(result["id"] == data["id"] and type(result["deleted"]) is bool)
        return
    _privacy_result(result, redact_sensitive=redact_sensitive)
    if kind in {"reviews", "experiments", "feedback"}:
        _scoped_result(result, kind, accept_media_omission=accept_media_omission)
    if operation == "list":
        _page(result, kind, data)
        return
    if operation == "export":
        _require(
            set(result)
            == {
                "schema_version",
                "document_id",
                "filename",
                "media_type",
                "encoding",
                "sha256",
                "content",
            }
        )
        _require(
            result["schema_version"] == "kuma.trajectory_export.v1"
            and result["document_id"] == data["id"]
            and result["filename"] == kind + ".json"
            and result["media_type"] == "application/json"
            and result["encoding"] == "gzip+base64"
        )
        _require(
            isinstance(result["content"], str)
            and result["sha256"] == export_digest(data["id"], kind)
        )
        return
    if operation == "compare":
        scoped = "contains_media_omission" in result
        if scoped:
            _require(
                accept_media_omission and result["contains_media_omission"] is True
            )
        _require(
            set(result)
            == {
                "schema_version",
                "comparisons",
                "gaps",
                "unlabeled_cases",
                "comparable_cases",
                "automatic_ranking",
                "counts_are",
                "normal_retries_are_penalties",
            }
            | ({"contains_media_omission"} if scoped else set())
            | ({"content_privacy"} if "content_privacy" in result else set())
        )
        _require(
            result["schema_version"] == "kuma.trajectory_comparison.v1"
            and result["automatic_ranking"] is None
            and result["normal_retries_are_penalties"] is False
        )
        return
    _receipt(result, kind)
    fields = {"id", "kind", "content_sha256", "summary"}
    if operation == "get":
        fields |= {"payload"}
        _require(result["id"] == data["id"] and isinstance(result["payload"], dict))
        encoded = json.dumps(
            result["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        _require(hashlib.sha256(encoded).hexdigest() == result["content_sha256"])
    elif action == "reviews.run":
        fields |= _review(result, data, accept_media_omission=accept_media_omission)
        if "content_privacy" in result:
            fields.add("content_privacy")
    _require(set(result) == fields)


def _privacy_result(result: dict, *, redact_sensitive: bool) -> None:
    """Gate only server wrapper markers; user source text never grants permission.

    Receipt-only writes/pages expose a safe summary marker. Full stored reads
    and flattened run responses must agree on raw lineage in both positions.
    Existing get validation separately binds payload bytes to the document hash.

    Args:
        redact_sensitive: Strict bool, default True. False explicitly retains original
            bounded content for this operation only; existing redaction cannot be undone.
    """
    records = result.get("items", [result])
    for record in records:
        summary = record.get("summary", {})
        _require(not scan_sensitive_json(summary, location="trajectory_summary"))
        payload = record.get("payload", record)
        marked = "content_privacy" in summary
        body_marked = "content_privacy" in payload
        if ("payload" in record or "review_status" in record) and scan_sensitive_json(
            payload, location="trajectory_payload"
        ):
            _require(marked and body_marked and not redact_sensitive)
        if marked or body_marked:
            _require(not redact_sensitive)
            if marked:
                _require(summary["content_privacy"] == "raw")
            if body_marked:
                _require(payload["content_privacy"] == "raw")
            if "payload" in record or "review_status" in record:
                _require(marked == body_marked)


def _scoped_result(result: dict, kind: str, *, accept_media_omission: bool) -> None:
    """Check only contract-bearing artifact slots, never guess scope from arbitrary user JSON.

    Server ownership/transitive-reference gates remain authoritative. The client checks
    supplied summary markers, persisted review coverage and experiment case markers;
    it cannot resolve a feedback review ID or inspect an absent source remotely here.
    """
    summaries = [result.get("summary", {})]
    summaries.extend(item.get("summary", {}) for item in result.get("items", []))
    payload = result.get("payload", result)
    for item in [*summaries, payload]:
        if "review_scope" in item:
            _require(
                accept_media_omission and item["review_scope"] == "text_and_structure"
            )
        if "contains_media_omission" in item:
            _require(accept_media_omission and item["contains_media_omission"] is True)
    if kind == "reviews":
        _scoped_review(result, payload, accept_media_omission=accept_media_omission)
    if kind == "experiments":
        for case in payload.get("cases", []):
            if "review_scope" in case:
                _require(
                    accept_media_omission
                    and case["review_scope"] == "text_and_structure"
                )


def _scoped_review(result: dict, payload: dict, *, accept_media_omission: bool) -> None:
    """Bind a review's scope, physical coverage and unknown-only public assessment.

    Shared by run/get and decoded export; immutable bytes and raw model audit are
    never changed. Missing or contradictory scope is rejected before presentation.
    """
    if isinstance(payload.get("report"), dict):
        _require(
            ("media_omission" in payload.get("coverage", {}))
            == ("media_omission" in payload["report"].get("coverage", {}))
        )
    if "coverage" in payload:
        _require(
            ("review_scope" in payload) == ("media_omission" in payload["coverage"])
        )
        if "summary" in result:
            _require(
                ("review_scope" in result["summary"]) == ("review_scope" in payload)
            )
        if "media_omission" in payload["coverage"]:
            _require(isinstance(payload.get("attempts"), list))
        _coverage(
            payload["coverage"],
            completed=payload["review_status"] == "completed",
            accept_media_omission=accept_media_omission,
            attempts=payload.get("attempts"),
        )
        if "media_omission" in payload["coverage"]:
            _require(payload.get("review_scope") == "text_and_structure")
            _require(payload["review_status"] in {"completed", "stopped"})
            _require(
                (payload["review_status"] == "completed")
                == isinstance(payload.get("report"), dict)
            )
            if isinstance(payload.get("report"), dict):
                _require(payload["report"].get("coverage") == payload["coverage"])
                validate_media_assessment(payload["report"])
    else:
        _require("review_scope" not in payload)


def validate_export_scope(
    raw: bytes, kind: str, *, accept_media_omission: bool, redact_sensitive: bool = True
) -> None:
    """Validate report-dependent decoded exports after digest verification without rewriting bytes.

    All workspace exports remain byte-exact but must be finite, unique-key JSON
    objects so their reserved privacy marker cannot be hidden from the reader.
    Report-dependent exports additionally require explicit opted-in media scope.
    Safe failures expose no payload/decoder detail; no external references are followed.

    Args:
        redact_sensitive: Strict bool, default True. False explicitly retains original
            bounded content for this operation only; existing redaction cannot be undone.
    """
    failed = False
    try:
        payload = detach_json(json.loads(raw, object_pairs_hook=_unique_object))
        _require(isinstance(payload, dict))
        if "content_privacy" in payload:
            _require(not redact_sensitive and payload["content_privacy"] == "raw")
        if scan_sensitive_json(payload, location="trajectory_export"):
            _require(not redact_sensitive and payload.get("content_privacy") == "raw")
        if kind in {"reviews", "experiments", "feedback"}:
            _scoped_result(
                {"payload": payload}, kind, accept_media_omission=accept_media_omission
            )
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        failed = True
    if failed:
        raise ProviderError("Invalid trajectory export scope", code="invalid_response")


def validate_response(
    value: dict[str, Any],
    action: str,
    data: dict[str, Any],
    *,
    accept_media_omission: bool = False,
    redact_sensitive: bool = True,
) -> dict[str, Any]:
    """Return a detached caller-owned result after shape, identity and revision checks.

    Args:
        value: Already finite/privacy-checked public envelope from Backend.
        action: Exact allowlisted action used for this request.
        data: The same admitted action input, used for response identity binding.
        accept_media_omission: Strict capability from the original request, default False.
            True permits closed text-and-structure coverage, never media understanding.
        redact_sensitive: Strict bool, default True. False explicitly retains original
            bounded content for this operation only; existing redaction cannot be undone.
    Returns:
        Public result mapping; metadata pages contain no source bodies. Details bind their payload hash.
    Raises:
        ProviderError: Any shape, category, identity, revision or completion mismatch, without values.
    Side Effects:
        None; caller owns any persistence, later transport or UI rendering.
    """
    failed, result = False, None
    try:
        _require(
            set(value) == {"schema_version", "result"}
            and value["schema_version"] == "kuma.trajectory_workspace.v1"
            and isinstance(value["result"], dict)
        )
        result = value["result"]
        _require(type(accept_media_omission) is bool and type(redact_sensitive) is bool)
        _result(
            result,
            action,
            data,
            accept_media_omission=accept_media_omission,
            redact_sensitive=redact_sensitive,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        failed = True
    if failed:
        raise ProviderError(
            "Invalid trajectory workspace response", code="invalid_response"
        )
    return result
