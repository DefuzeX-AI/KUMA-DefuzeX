"""Closed assessment Evidence validation shared by local capture and upload."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from itertools import pairwise
from typing import Any

from .._json_values import detach_json
from ..errors import LimitExceededError, ValidationError
from ..repository.privacy import redact_sensitive_text, scan_sensitive_json
from .public_messages import MAX_MESSAGE_BYTES, MAX_PUBLIC_MESSAGES, SOURCE_ACTORS

ASSESSMENT_CONTRACT = "kuma.judge_assessment.v1"
ASSESSMENT_EVIDENCE_SCHEMA = "defuzex.assessment_evidence.v1"
ASSESSMENT_EVIDENCE_MEDIA_TYPE = "application/vnd.defuzex.assessment-evidence+json"
ASSESSMENT_EVIDENCE_MAX_BYTES = 1048576
COVERAGE_REASONS = frozenset(
    {"source_incomplete", "redacted", "unsupported_event", "unfinished_message"}
)
_HASH = re.compile(r"[0-9a-f]{64}")


def assessment_json(value: Any) -> bytes:
    """Encode a detached finite JSON graph deterministically for hash/byte identity."""
    return json.dumps(
        detach_json(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fields(value: Any, names: set[str]) -> bool:
    """Return whether a mapping contains exactly the declared public fields."""
    return isinstance(value, Mapping) and set(value) == names


def _integer(value: Any) -> bool:
    """Accept nonnegative integer source indexes, excluding booleans."""
    return type(value) is int and value >= 0


def _text(value: Any, maximum: int, *, empty: bool = False) -> bool:
    """Bound protocol text independently of message bodies."""
    return isinstance(value, str) and int(not empty) <= len(value) <= maximum


def valid_reference(value: Any) -> bool:
    """Validate a closed hash-bound reference without resolving external data.

    Core resolves these pointers only against accepted Evidence. SDK checks
    shape, index and RFC6901 escaping; it never opens filesystem/DB references
    or interprets structural validity as proof of an Agent behavior.
    """
    return (
        _fields(value, {"evidence_index", "evidence_sha256", "pointer"})
        and _integer(value["evidence_index"])
        and value["evidence_index"] <= 49
        and isinstance(value["evidence_sha256"], str)
        and _HASH.fullmatch(value["evidence_sha256"]) is not None
        and _text(value["pointer"], 512, empty=True)
        and (value["pointer"] == "" or value["pointer"].startswith("/"))
        and re.search(r"~(?![01])", value["pointer"]) is None
    )


def _validated_message(message: Any) -> tuple[dict[str, Any], bool]:
    """Check source coordinates, sanitize text, and recompute its retained hash."""
    names = {
        "message_id",
        "sequence",
        "source_event_index",
        "source_event_id",
        "actor",
        "content",
        "content_sha256",
    }
    if isinstance(message, Mapping):
        message = {"actor": "unknown", **message}
    if not _fields(message, names):
        raise ValueError("Invalid public message")
    if (
        not _text(message["message_id"], 128)
        or not _integer(message["sequence"])
        or not _integer(message["source_event_index"])
        or not (
            message["source_event_id"] is None or _text(message["source_event_id"], 128)
        )
        or not isinstance(message["actor"], str)
        or message["actor"] not in SOURCE_ACTORS
        or not isinstance(message["content"], str)
        or not message["content"]
    ):
        raise ValueError("Invalid public message")
    content = message["content"].encode("utf-8")
    if len(content) > MAX_MESSAGE_BYTES:
        raise LimitExceededError(
            "Public message exceeds 32768 UTF-8 bytes", code="public_messages_too_large"
        )
    if hashlib.sha256(content).hexdigest() != message["content_sha256"]:
        raise ValueError("Invalid public message digest")
    safe = redact_sensitive_text(message["content"])
    return {
        **message,
        "content": safe,
        "content_sha256": hashlib.sha256(safe.encode("utf-8")).hexdigest(),
    }, safe != message["content"]


def validate_public_messages(value: Any) -> dict[str, Any]:
    """Detach and validate the caller's message bundle before Run side effects.

    Accept public_messages, coverage and optional canonical provenance; reject oversized input instead
    of truncating. Reuse canonical redaction even for hand-built bundles, forcing
    partial capture when any text changes. Unknown actors remain unknown. No
    paths, credentials, tools or network are accessed and errors omit values.
    """
    try:
        result = _validate_bundle(detach_json(value))
    except LimitExceededError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        result = None
    if result is None:
        raise ValidationError(
            "Public messages are invalid", code="public_messages_invalid"
        )
    return result


def _validate_bundle(value: Any) -> dict[str, Any]:
    """Validate ordered message identities and explicit independent coverage."""
    if not isinstance(value, Mapping) or set(value) not in (
        {"public_messages", "coverage"},
        {"public_messages", "coverage", "provenance"},
    ):
        raise ValueError("Invalid message bundle")
    messages, coverage = value["public_messages"], value["coverage"]
    if not isinstance(messages, list):
        raise ValueError("Invalid messages")
    if len(messages) > MAX_PUBLIC_MESSAGES:
        raise LimitExceededError(
            "Public message count exceeds 200", code="public_messages_too_large"
        )
    _validate_coverage(coverage)
    result = _validated_messages(messages, coverage)
    if "provenance" in value:
        result["provenance"] = _validated_provenance(value["provenance"])
    return result


def _validated_provenance(value: Any) -> list[dict[str, Any]]:
    """Validate caller-declared accepted-Evidence references, never infer actors.

    References are checked against actual serialized runtime parts before upload.
    This local syntax check does not resolve files or verify the declaration's
    truth. Unknown fields, duplicate references and sensitive metadata fail closed.
    """
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("Invalid provenance")
    seen = set()
    for entry in value:
        if (
            not _fields(entry, {"reference", "actor"})
            or not valid_reference(entry["reference"])
            or not isinstance(entry["actor"], str)
            or entry["actor"] not in SOURCE_ACTORS
        ):
            raise ValueError("Invalid provenance")
        identity = assessment_json(entry["reference"])
        if identity in seen:
            raise ValueError("Duplicate provenance")
        seen.add(identity)
    if scan_sensitive_json(value, location="provenance"):
        raise ValueError("Unsafe provenance")
    _consistent_provenance(value)
    return value


def _consistent_provenance(value: list[dict[str, Any]]) -> None:
    """Reject overlapping actor contradictions independent of declaration order."""
    actors = {
        (entry["reference"]["evidence_index"], entry["reference"]["pointer"]): entry[
            "actor"
        ]
        for entry in value
    }
    for (index, pointer), actor in actors.items():
        segments = pointer.split("/")
        for length in range(1, len(segments)):
            ancestor = (index, "/".join(segments[:length]))
            if ancestor in actors and actors[ancestor] != actor:
                raise ValueError("Conflicting provenance")


def _validate_coverage(coverage: Any) -> None:
    """Require independent closed coverage axes and unique stable gap reasons."""
    if (
        not _fields(coverage, {"public_messages", "execution", "network", "reasons"})
        or any(
            coverage[name] not in ("complete", "partial", "unavailable")
            for name in ("public_messages", "execution", "network")
        )
        or not isinstance(coverage["reasons"], list)
        or any(
            not isinstance(x, str) or x not in COVERAGE_REASONS
            for x in coverage["reasons"]
        )
        or len(coverage["reasons"]) != len(set(coverage["reasons"]))
        or (coverage["public_messages"] == "complete" and bool(coverage["reasons"]))
    ):
        raise ValueError("Invalid capture coverage")


def _validated_messages(
    messages: list[Any], coverage: dict[str, Any]
) -> dict[str, Any]:
    """Detach ordered source coordinates and make every redaction a coverage gap."""
    ids, sequences, indexes = set(), [], []
    safe_messages = []
    reasons = set(coverage["reasons"])
    for message in messages:
        safe, redacted = _validated_message(message)
        if safe["message_id"] in ids:
            raise ValueError("Duplicate message ID")
        ids.add(safe["message_id"])
        sequences.append(safe["sequence"])
        indexes.append(safe["source_event_index"])
        if redacted:
            reasons.add("redacted")
        safe_messages.append(safe)
    if any(b <= a for values in (sequences, indexes) for a, b in pairwise(values)):
        raise ValueError("Unordered messages")
    status = coverage["public_messages"]
    if reasons and status == "complete":
        status = "partial"
    result = {
        "public_messages": safe_messages,
        "coverage": {**coverage, "public_messages": status, "reasons": sorted(reasons)},
    }
    if scan_sensitive_json(result, location="public_messages"):
        raise ValueError("Unsafe public message metadata")
    if len(assessment_json(result)) > ASSESSMENT_EVIDENCE_MAX_BYTES:
        raise LimitExceededError(
            "Public messages exceed 1 MiB", code="public_messages_too_large"
        )
    return result


def build_assessment_evidence(
    bundle: Any,
    *,
    run_id: str,
    input_id: str,
    step_id: str,
    submission_id: str,
) -> dict[str, Any]:
    """Associate a validated bundle with exactly one committed Submission.

    Run calls this before capture/persistence. Provenance defaults empty: message
    actors are explicit declarations, and unrelated execution is not guessed.
    Upload revalidates this envelope; Core owns accepted-reference resolution.
    Raises stable local validation/size errors without copying raw input values.
    """
    if not all(
        _text(value, maximum)
        for value, maximum in (
            (run_id, 128),
            (input_id, 128),
            (step_id, 80),
            (submission_id, 128),
        )
    ):
        raise ValidationError(
            "Invalid assessment association", code="public_messages_invalid"
        )
    result = {
        "schema_version": ASSESSMENT_EVIDENCE_SCHEMA,
        "run_id": run_id,
        "input_id": input_id,
        "step_id": step_id,
        "submission_id": submission_id,
        "provenance": [],
        **validate_public_messages(bundle),
    }
    if len(assessment_json(result)) > ASSESSMENT_EVIDENCE_MAX_BYTES:
        raise LimitExceededError(
            "Assessment Evidence exceeds 1 MiB", code="public_messages_too_large"
        )
    return result
