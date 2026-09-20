"""Normalize completed public native messages without executing Agent events."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from ..errors import LimitExceededError, ValidationError
from ..repository.privacy import redact_sensitive_text, scan_sensitive_text

SOURCE_ACTORS = frozenset(
    {
        "target_agent",
        "sdk_setup",
        "external_evaluator",
        "reviewer",
        "cross_case_reference",
        "unknown",
    }
)
MAX_PUBLIC_MESSAGES = 200
MAX_MESSAGE_BYTES = 32768
MAX_SOURCE_EVENTS = 100000
_EVENT_TYPES = frozenset(
    {
        "item.started",
        "item.updated",
        "item.completed",
        "session.started",
        "turn.started",
        "turn.completed",
        "exec.started",
        "exec.completed",
    }
)


def _invalid_events() -> ValidationError:
    """Return a fixed local error without retaining source values or exceptions."""
    return ValidationError(
        "Public Agent events are invalid", code="public_messages_invalid"
    )


def _identifier(value: Any) -> bool:
    """Accept bounded source identifiers without paths, controls or known secrets."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(char.isascii() and (char.isalnum() or char in "._:-") for char in value)
        and not scan_sensitive_text(value, location="source_identifier")
    )


def _message(
    event: dict[str, Any], index: int, actor: str
) -> tuple[dict[str, Any], bool]:
    """Project one completed native item with distinct index and event sequence.

    The native-stream caller has established completion/type. Content is bounded
    before the shared redactor runs; its digest describes retained text, never an
    omitted secret. Source metadata is validated, not rewritten into false IDs.
    No filesystem, process, credentials or network are accessed.
    """
    item = event["item"]
    sequence, identifier, content = (
        event.get("sequence"),
        item.get("id"),
        item.get("content"),
    )
    if (
        type(sequence) is not int
        or sequence < 0
        or not _identifier(identifier)
        or not isinstance(content, str)
        or not content
    ):
        raise _invalid_events()
    try:
        encoded = content.encode("utf-8")
    except UnicodeError:
        raise _invalid_events() from None
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise LimitExceededError(
            "Public message exceeds 32768 UTF-8 bytes", code="public_messages_too_large"
        )
    safe = redact_sensitive_text(content)
    return {
        "message_id": f"message-{index}",
        "sequence": sequence,
        "source_event_index": index,
        "source_event_id": identifier,
        "actor": actor,
        "content": safe,
        "content_sha256": hashlib.sha256(safe.encode("utf-8")).hexdigest(),
    }, safe != content


def _completed_public(event: Any, reasons: set[str], pending: set[str]) -> bool:
    """Classify native events without reading excluded reasoning/tool content.

    Known lifecycle events are not missing messages. Unsupported shapes mark
    coverage partial; unfinished public IDs remain pending until completion.
    """
    if not isinstance(event, dict):
        raise _invalid_events()
    event_type = event.get("type")
    if (
        type(event.get("schemaVersion")) is not int
        or event["schemaVersion"] != 1
        or not isinstance(event_type, str)
        or event_type not in _EVENT_TYPES
    ):
        reasons.add("unsupported_event")
        return False
    item = event.get("item")
    if not event_type.startswith("item."):
        return False
    if not isinstance(item, dict) or item.get("type") not in (
        "agent_message",
        "reasoning",
        "tool_call",
    ):
        reasons.add("unsupported_event")
        return False
    if item["type"] != "agent_message":
        return False
    identifier = item.get("id")
    if event_type == "item.completed":
        if _identifier(identifier):
            pending.discard(identifier)
        return True
    if _identifier(identifier):
        pending.add(identifier)
    else:
        reasons.add("unfinished_message")
    return False


def collect_public_messages(
    events: Sequence[dict[str, Any]],
    *,
    source_actor: str = "unknown",
    source_complete: bool = False,
) -> dict[str, Any]:
    """Collect every completed public message from a supported native stream.

    Args:
        events: Ordered in-memory native events (at most 100000). The supported
            shape has integer schemaVersion=1, type=item.completed, and
            item={id, type:agent_message, content:str}. Other known lifecycle,
            tool and reasoning events are not public messages. No JSONL file is
            opened and no event command is executed.
        source_actor: Declared origin of this one stream: target_agent,
            sdk_setup, external_evaluator, reviewer, cross_case_reference or
            unknown (default). Use target_agent only after the runner isolates
            the current target's stream; this declaration is not verified truth.
        source_complete: False by default. True asserts the supplied stream is
            complete for this invocation, not that execution or network capture
            is complete. Redaction, unsupported or unfinished events override it.

    Returns:
        Detached public_messages and coverage mapping for Run.submit. Each
        message retains original sequence, zero-based source_event_index and
        source item ID. Text redaction changes its hash and records a coverage
        gap. Execution/network coverage remains unavailable.

    Raises:
        ValidationError: Invalid sequence, message metadata or input shape.
        LimitExceededError: More than 200 messages, 100000 events, or a message
            larger than 32768 UTF-8 bytes. Oversize text is never truncated.

    Postconditions:
        Input is unchanged; reasoning/unfinished content is never copied.
        Complete means complete public-message capture, not complete claim
        extraction, successful execution, or proof of no network upload.

    Side Effects:
        None. Only bounded in-memory data is inspected.

    Security/Privacy:
        Uses the canonical redactor with explicit redacted coverage. Unknown
        secrets are not guaranteed detectable; diagnostics contain no payload.
    """
    if (
        not isinstance(events, (list, tuple))
        or not isinstance(source_actor, str)
        or source_actor not in SOURCE_ACTORS
        or type(source_complete) is not bool
    ):
        raise _invalid_events()
    if len(events) > MAX_SOURCE_EVENTS:
        raise LimitExceededError(
            "Native event count exceeds 100000", code="public_messages_too_large"
        )
    messages: list[dict[str, Any]] = []
    reasons = set() if source_complete else {"source_incomplete"}
    pending: set[str] = set()
    previous = -1
    for index, event in enumerate(events):
        if not _completed_public(event, reasons, pending):
            continue
        message, redacted = _message(event, index, source_actor)
        if message["sequence"] <= previous:
            raise _invalid_events()
        previous = message["sequence"]
        if redacted:
            reasons.add("redacted")
        messages.append(message)
        if len(messages) > MAX_PUBLIC_MESSAGES:
            raise LimitExceededError(
                "Public message count exceeds 200", code="public_messages_too_large"
            )
    if pending:
        reasons.add("unfinished_message")
    return {
        "public_messages": messages,
        "coverage": {
            "public_messages": "partial" if reasons else "complete",
            "execution": "unavailable",
            "network": "unavailable",
            "reasons": sorted(reasons),
        },
    }
