"""Snapshot comparison and optional text unified diffs."""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ...contracts import FileChange, FileEvidence
from .snapshot import Snapshot, SnapshotEntry


@dataclass(frozen=True, slots=True)
class DiffResult:
    evidence: FileEvidence
    local_diffs: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "local_diffs", MappingProxyType(dict(self.local_diffs))
        )


def _rename_key(entry: SnapshotEntry) -> tuple[str, int, str] | None:
    if not entry.hash_complete or entry.sha256 is None:
        return None
    return entry.file_type, entry.size, entry.sha256


def _changed(before: SnapshotEntry, after: SnapshotEntry) -> bool:
    return any(
        (
            before.file_type != after.file_type,
            before.mode != after.mode,
            before.size != after.size,
            before.mtime_ns != after.mtime_ns,
            before.sha256 != after.sha256,
            before.hash_complete != after.hash_complete,
            before.scan_error != after.scan_error,
        )
    )


def _reason(before: SnapshotEntry | None, after: SnapshotEntry | None) -> str | None:
    reasons: list[str] = []
    for entry in (before, after):
        if entry is not None and entry.scan_error and entry.scan_error not in reasons:
            reasons.append(entry.scan_error)
    if before is not None and after is not None and before.file_type != after.file_type:
        reasons.append("type_changed")
    return ",".join(reasons) or None


def _unified_diff(
    before: SnapshotEntry | None,
    after: SnapshotEntry | None,
    *,
    old_path: str,
    new_path: str,
) -> str | None:
    before_text = "" if before is None else before.text_content
    after_text = "" if after is None else after.text_content
    if before_text is None or after_text is None:
        return None
    if before_text == after_text:
        return None
    return "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=old_path if before is not None else "/dev/null",
            tofile=new_path if after is not None else "/dev/null",
        )
    )


def _renamed_changes(
    removed: dict[str, SnapshotEntry], added: dict[str, SnapshotEntry]
) -> list[FileChange]:
    removed_by_key: dict[tuple[str, int, str], list[str]] = {}
    added_by_key: dict[tuple[str, int, str], list[str]] = {}
    for path, entry in removed.items():
        key = _rename_key(entry)
        if key is not None:
            removed_by_key.setdefault(key, []).append(path)
    for path, entry in added.items():
        key = _rename_key(entry)
        if key is not None:
            added_by_key.setdefault(key, []).append(path)

    changes: list[FileChange] = []
    for key in sorted(removed_by_key.keys() & added_by_key.keys()):
        old_paths = removed_by_key[key]
        new_paths = added_by_key[key]
        if len(old_paths) != 1 or len(new_paths) != 1:
            continue
        old_path, new_path = old_paths[0], new_paths[0]
        old_entry, new_entry = removed.pop(old_path), added.pop(new_path)
        changes.append(
            FileChange(
                path=new_path,
                old_path=old_path,
                change_type="renamed",
                file_type=new_entry.file_type,
                before_hash=old_entry.sha256,
                after_hash=new_entry.sha256,
                before_size=old_entry.size,
                after_size=new_entry.size,
                before_mode=old_entry.mode,
                after_mode=new_entry.mode,
                complete=old_entry.hash_complete and new_entry.hash_complete,
                reason=_reason(old_entry, new_entry),
            )
        )
    return changes


def _deleted_changes(
    removed: Mapping[str, SnapshotEntry], *, upload_diff: bool
) -> tuple[list[FileChange], dict[str, str]]:
    changes: list[FileChange] = []
    local_diffs: dict[str, str] = {}
    for path in sorted(removed):
        entry = removed[path]
        text_diff = _unified_diff(entry, None, old_path=path, new_path=path)
        if text_diff is not None:
            local_diffs[path] = text_diff
        changes.append(
            FileChange(
                path=path,
                change_type="deleted",
                file_type=entry.file_type,
                before_hash=entry.sha256,
                before_size=entry.size,
                before_mode=entry.mode,
                complete=entry.hash_complete,
                reason=_reason(entry, None),
                diff=text_diff if upload_diff else None,
            )
        )
    return changes, local_diffs


def _created_changes(
    added: Mapping[str, SnapshotEntry], *, upload_diff: bool
) -> tuple[list[FileChange], dict[str, str]]:
    changes: list[FileChange] = []
    local_diffs: dict[str, str] = {}
    for path in sorted(added):
        entry = added[path]
        text_diff = _unified_diff(None, entry, old_path=path, new_path=path)
        if text_diff is not None:
            local_diffs[path] = text_diff
        changes.append(
            FileChange(
                path=path,
                change_type="created",
                file_type=entry.file_type,
                after_hash=entry.sha256,
                after_size=entry.size,
                after_mode=entry.mode,
                complete=entry.hash_complete,
                reason=_reason(None, entry),
                diff=text_diff if upload_diff else None,
            )
        )
    return changes, local_diffs


def _modified_changes(
    before: Snapshot,
    after: Snapshot,
    common: set[str],
    *,
    upload_diff: bool,
) -> tuple[list[FileChange], dict[str, str]]:
    changes: list[FileChange] = []
    local_diffs: dict[str, str] = {}
    for path in sorted(common):
        old_entry, new_entry = before.entries[path], after.entries[path]
        if not _changed(old_entry, new_entry):
            continue
        text_diff = _unified_diff(old_entry, new_entry, old_path=path, new_path=path)
        if text_diff is not None:
            local_diffs[path] = text_diff
        changes.append(
            FileChange(
                path=path,
                change_type="modified",
                file_type=new_entry.file_type,
                before_hash=old_entry.sha256,
                after_hash=new_entry.sha256,
                before_size=old_entry.size,
                after_size=new_entry.size,
                before_mode=old_entry.mode,
                after_mode=new_entry.mode,
                complete=old_entry.hash_complete and new_entry.hash_complete,
                reason=_reason(old_entry, new_entry),
                diff=text_diff if upload_diff else None,
            )
        )
    return changes, local_diffs


def compare_snapshots(
    before: Snapshot,
    after: Snapshot,
    *,
    scope: str,
    upload_diff: bool,
) -> DiffResult:
    removed = {
        path: before.entries[path]
        for path in before.entries.keys() - after.entries.keys()
    }
    added = {
        path: after.entries[path]
        for path in after.entries.keys() - before.entries.keys()
    }
    common = before.entries.keys() & after.entries.keys()
    changes = _renamed_changes(removed, added)
    deleted, deleted_diffs = _deleted_changes(removed, upload_diff=upload_diff)
    created, created_diffs = _created_changes(added, upload_diff=upload_diff)
    modified, modified_diffs = _modified_changes(
        before, after, set(common), upload_diff=upload_diff
    )
    changes.extend((*deleted, *created, *modified))
    local_diffs = {**deleted_diffs, **created_diffs, **modified_diffs}

    errors = tuple(dict.fromkeys((*before.errors, *after.errors)))
    return DiffResult(
        evidence=FileEvidence(
            complete=before.complete
            and after.complete
            and all(item.complete for item in changes),
            scope=scope,
            changes=tuple(changes),
            errors=errors,
            extensions={
                "diff_available_count": len(local_diffs),
                "diff_included_count": len(local_diffs) if upload_diff else 0,
            },
        ),
        local_diffs=local_diffs,
    )


__all__ = ["DiffResult", "compare_snapshots"]
