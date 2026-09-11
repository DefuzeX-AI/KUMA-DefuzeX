"""Validate the negotiated file-diff portion of Runtime Evidence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

RUNTIME_FILE_DIFF_MAX_BYTES = 32_768
RUNTIME_FILE_DIFF_TOTAL_MAX_BYTES = 65_536
RUNTIME_FILE_DIFF_OMISSION_REASONS = frozenset(
    {
        "binary",
        "size_limit",
        "sensitive_content",
        "no_text_change",
        "capture_incomplete",
    }
)
_UNIFIED_HUNK_HEADER = re.compile(
    r"^@@ -(0|[1-9][0-9]*)(?:,(0|[1-9][0-9]*))? "
    r"\+(0|[1-9][0-9]*)(?:,(0|[1-9][0-9]*))? @@(?: .*)?$"
)


def _valid_unified_hunks(lines: list[str]) -> bool:
    """Return whether complete unified-diff hunks match their declared counts.

    Args:
        lines: Diff lines after the exact old/new path headers.

    Returns:
        ``True`` only when every hunk has canonical syntax, valid body prefixes,
        at least one change, and exact source/destination line counts.

    Side Effects:
        None; validation never executes or applies the patch.
    """

    index = 0
    while index < len(lines):
        match = _UNIFIED_HUNK_HEADER.fullmatch(lines[index])
        if match is None:
            return False
        expected_old = int(match.group(2)) if match.group(2) is not None else 1
        expected_new = int(match.group(4)) if match.group(4) is not None else 1
        old_count = new_count = body_count = 0
        changed = False
        previous_was_body = False
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line == r"\ No newline at end of file":
                if not previous_was_body:
                    return False
                previous_was_body = False
                index += 1
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                return False
            prefix = line[0]
            old_count += prefix in {" ", "-"}
            new_count += prefix in {" ", "+"}
            body_count += 1
            changed = changed or prefix in {"+", "-"}
            previous_was_body = True
            if old_count > expected_old or new_count > expected_new:
                return False
            index += 1
        if (
            body_count == 0
            or not changed
            or old_count != expected_old
            or new_count != expected_new
        ):
            return False
    return True


def _valid_unified_diff(component: Mapping[str, Any], value: Any) -> bool:
    """Validate one complete, hash-bound diff against its file-change path.

    Args:
        component: Owning closed ``file_change`` component.
        value: Candidate ``diff`` object.

    Returns:
        ``True`` only for an exact unified diff within 32 KiB whose digest,
        byte count, syntax, and old/new headers match the owning path.

    Security/Privacy:
        The validator treats text as data and never applies it or reads files.
    """

    if not isinstance(value, Mapping) or set(value) != {
        "format",
        "text",
        "text_sha256",
        "utf8_bytes",
    }:
        return False
    text = value.get("text")
    if value.get("format") != "unified" or not isinstance(text, str) or not text:
        return False
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if (
        b"\x00" in encoded
        or not 1 <= len(encoded) <= RUNTIME_FILE_DIFF_MAX_BYTES
        or value.get("utf8_bytes") != len(encoded)
        or value.get("text_sha256") != hashlib.sha256(encoded).hexdigest()
    ):
        return False
    lines = text.splitlines()
    if len(lines) < 3:
        return False
    path = component["path"]
    expected_before = "/dev/null" if component["change_type"] == "created" else path
    expected_after = "/dev/null" if component["change_type"] == "deleted" else path
    return (
        component["change_type"] != "unchanged"
        and lines[0] == f"--- {expected_before}"
        and lines[1] == f"+++ {expected_after}"
        and _valid_unified_hunks(lines[2:])
    )


def _valid_file_diff_state(component: Mapping[str, Any], *, enabled: bool) -> bool:
    """Bind file-change diff fields to explicit capability negotiation.

    Args:
        component: Closed file-change mapping.
        enabled: Whether the envelope declared ``file_diff``.

    Returns:
        ``True`` when hash-only components contain neither outcome, or enabled
        components contain exactly one valid diff or frozen omission reason.
    """

    has_diff = "diff" in component
    has_reason = "diff_omission_reason" in component
    if not enabled:
        return not has_diff and not has_reason
    if has_diff == has_reason:
        return False
    if has_reason:
        return component["diff_omission_reason"] in RUNTIME_FILE_DIFF_OMISSION_REASONS
    return _valid_unified_diff(component, component["diff"])


def validate_file_diff_components(
    components: Sequence[Mapping[str, Any]], *, enabled: bool
) -> None:
    """Validate negotiated file outcomes and their aggregate byte ceiling.

    Args:
        components: Already closed, ordered Runtime Evidence components.
        enabled: Whether the envelope declared ``file_diff``.

    Raises:
        ValueError: If an outcome conflicts with negotiation, is malformed, or
            included diff bytes exceed 65,536 in one envelope.

    Side Effects:
        None; component mappings are never mutated.
    """

    file_components = [item for item in components if item["kind"] == "file_change"]
    if any(
        not _valid_file_diff_state(item, enabled=enabled) for item in file_components
    ):
        raise ValueError("runtime evidence file diff is invalid")
    total = sum(item.get("diff", {}).get("utf8_bytes", 0) for item in file_components)
    if total > RUNTIME_FILE_DIFF_TOTAL_MAX_BYTES:
        raise ValueError("runtime evidence file diffs exceed the byte limit")


__all__ = [
    "RUNTIME_FILE_DIFF_MAX_BYTES",
    "RUNTIME_FILE_DIFF_OMISSION_REASONS",
    "RUNTIME_FILE_DIFF_TOTAL_MAX_BYTES",
    "validate_file_diff_components",
]
