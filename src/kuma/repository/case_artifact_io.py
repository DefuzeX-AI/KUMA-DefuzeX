"""Bounded local Case files; no-overwrite publication and verified read handles."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError, ValidationError
from ..evidence.tracking._log_paths import (
    _verify_open_descriptor,
    open_verified_log,
    resolve_log_path,
)
from ._case_file_directory import pinned_case_directory
from .case_artifacts import MAX_CASE_ARTIFACT_BYTES, validate_artifact
from .privacy import enforce_sensitive_policy, scan_sensitive_path


def _path(root: Path, supplied: str | os.PathLike[str], alias: Path) -> Path:
    """Reuse repository containment/link/mount checks for one explicitly selected file."""
    path, _, rejected = resolve_log_path(root, supplied, index=0, root_alias=alias)
    if rejected is not None or path is None or path == root:
        raise ConfigurationError(
            "Case file path must stay inside the repository without links"
        )
    enforce_sensitive_policy(
        scan_sensitive_path(path, location="case_artifact"), allow_sensitive=False
    )
    return path


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate JSON keys before artifact schema validation."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def load_case_artifact(
    root: Path, supplied: str | os.PathLike[str], *, alias: Path
) -> dict[str, Any]:
    """Read one explicit Case file before any Run or credential setup.

    Args:
        root: Validated canonical Run repository.
        supplied: Explicit file; relative paths are rooted at root, never cwd.
        alias: Original absolute root spelling, preserving macOS path aliases.
    Returns:
        Complete validated artifact; no source guessing or inputs-only fallback.
    Raises:
        ConfigurationError: Safe path/open errors without host error chains.
        ValidationError: Invalid UTF-8/duplicate JSON/oversize/invalid artifact.
    Side Effects:
        Reads at most 5 MiB plus one sentinel byte from a verified regular handle.
        No writes, schema fetch, credentials, or network operations.
    """
    path = _path(root, supplied, alias)
    failed = False
    try:
        handle, _ = open_verified_log(root, path)
        with handle:
            raw = handle.read(MAX_CASE_ARTIFACT_BYTES + 1)
    except OSError:
        failed = True
    if failed:
        raise ConfigurationError("Case file is missing or unreadable")
    invalid = len(raw) > MAX_CASE_ARTIFACT_BYTES
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        invalid = True
    if invalid:
        raise ValidationError(
            "Case file must contain bounded UTF-8 JSON", code="case_artifact_invalid"
        )
    return validate_artifact(data)


def _publish(
    root: Path, destination: Path, encoded: bytes, parent_fd: int | None
) -> None:
    """Write/fsync/link one temporary inode under a pinned destination parent.

    The file descriptor stays open through publication. Relative POSIX operations
    cannot be redirected by a later parent symlink; Windows pins its ancestors.
    Check inode identity before linking and after publication, close ownership
    on fdopen failure, and remove temporary/failed publication names on exit.
    No existing destination is ever replaced. OSError is mapped by save's public
    boundary after cleanup, so native exception chains never reach callers.
    """
    descriptor: int | None = None
    temporary: Path | None = None
    published = False
    complete = False
    created = False
    options = {} if parent_fd is None else {"dir_fd": parent_fd}
    target = destination if parent_fd is None else destination.name
    try:
        if parent_fd is None:
            descriptor, name = tempfile.mkstemp(
                prefix=".kuma-case-", suffix=".tmp", dir=destination.parent
            )
            temporary = Path(name)
        else:
            temporary = destination.parent / f".kuma-case-{secrets.token_hex(16)}.tmp"
            descriptor = os.open(
                temporary.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        created = True
        frozen = os.fstat(descriptor)
        _verify_open_descriptor(root, temporary, frozen)
        source = temporary if parent_fd is None else temporary.name
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            _verify_open_descriptor(root, temporary, frozen)
            link_options = (
                {}
                if parent_fd is None
                else {"src_dir_fd": parent_fd, "dst_dir_fd": parent_fd}
            )
            os.link(source, target, follow_symlinks=False, **link_options)
            published = True
            named = os.stat(target, follow_symlinks=False, **options)
            if (named.st_dev, named.st_ino) != (frozen.st_dev, frozen.st_ino):
                raise OSError("Case publication identity changed")
            _verify_open_descriptor(root, destination, frozen)
            complete = True
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            try:
                if published and not complete:
                    os.unlink(target, **options)
            finally:
                if created and temporary is not None:
                    with suppress(FileNotFoundError):
                        os.unlink(
                            temporary if parent_fd is None else temporary.name,
                            **options,
                        )


def save_case_artifact(
    root: Path, supplied: str | os.PathLike[str], data: dict[str, Any]
) -> Path:
    """Atomically publish one reviewed public Case without overwriting any file.

    Args:
        root: Existing canonical repository, owned by the Run.
        supplied: Explicit destination inside root; parent must already exist.
        data: Validated reusable artifact, never History/Submission/Evidence.
    Returns:
        Absolute path to the newly published complete JSON file.
    Raises:
        ConfigurationError: Existing/unsafe target or local I/O failure.
        ValidationError: Artifact size, content, or origin is invalid.
    Postconditions:
        Publication uses atomic hard-link creation (no replacement). Concurrent
        writers cannot overwrite a winner. Temporary descriptors/files are closed
        and cleaned on failure; unsupported filesystems fail without fallback.
    Side Effects:
        Writes/fsyncs one verified temporary sibling, links it to the destination,
        and unlinks the temporary name. No directories or runtime state are created.
    """
    validated = validate_artifact(data)
    encoded = json.dumps(
        validated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    destination = _path(root, supplied, root)
    failed = False
    try:
        with pinned_case_directory(root, destination.parent) as parent_fd:
            _publish(root, destination, encoded, parent_fd)
    except (OSError, ValueError):
        failed = True
    if failed:
        raise ConfigurationError(
            "Case file could not be saved; choose a new accessible path"
        )
    return destination
