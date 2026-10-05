"""Local-only selection of artifact storage independently of tracked files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .errors import ConfigurationError


def _validate_owned_paths(root: Path, device: int) -> None:
    """Reject unsafe pre-existing SDK directories before any storage-side write.

    The external root may itself be a dedicated volume. Descendant mounts and
    links are not allowed to redirect runtime/ledger output onto another target.
    Native errors are translated by resolve_storage_path; nothing is created.
    """
    for relative in (
        ".kuma",
        ".kuma/runtime",
        ".kuma/runs",
        ".kuma/requests",
        ".kuma/reports",
    ):
        child = root / relative
        if not child.exists() and not child.is_symlink():
            continue
        metadata = child.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & 0x400
            or metadata.st_dev != device
            or child.resolve() != child
        ):
            raise ValueError


def resolve_storage_path(
    repo_path: Path, storage_path: str | os.PathLike[str] | None
) -> Path:
    """Validate an artifact parent before callers perform network or local writes.

    Args:
        repo_path: Existing canonical tracked repository directory.
        storage_path: Existing external directory, relative to repo_path or
            absolute. None retains repository-local storage.
    Returns:
        Canonical artifact parent; SDK runtime data lives in its .kuma child.
    Raises:
        ConfigurationError: Invalid, inaccessible, linked, filesystem-root or
            overlapping external destination. Native path details are suppressed.
    Side Effects:
        Reads directory metadata only; creates nothing and never falls back.
    Security/Privacy:
        This selects storage, not an Agent sandbox. Callers must independently
        restrict Agent access to artifacts, credentials and process state.
    """
    if storage_path is None:
        return repo_path
    try:
        if not isinstance(storage_path, (str, os.PathLike)) or not os.fspath(
            storage_path
        ):
            raise ValueError
        path = Path(storage_path).expanduser()
        if not path.is_absolute():
            path = repo_path / path
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError
        root = path.resolve(strict=True)
        if (
            not root.is_dir()
            or root == Path(root.anchor)
            or root == repo_path
            or root in repo_path.parents
            or repo_path in root.parents
        ):
            raise ValueError
        _validate_owned_paths(root, info.st_dev)
        return root
    except (TypeError, ValueError, OSError, RuntimeError):
        pass
    raise ConfigurationError(
        "storage_path must be an existing external directory disjoint from repo_path"
    ) from None
