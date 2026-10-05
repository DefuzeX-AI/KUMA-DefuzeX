"""Filesystem transaction boundary for the repository-local request ledger."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, BinaryIO

from .._runtime_filesystem import ensure_repo_runtime_directory
from ..errors import ConfigurationError, LimitExceededError, ProviderError
from ._request_contract import MAX_PUBLIC_REPORT_BYTES, MAX_RECORDS

_PROCESS_LOCK = threading.RLock()


def canonical_repo_root(value: Path) -> Path:
    """Resolve an existing repository directory or raise a path-safe error."""
    try:
        root = value.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise OSError
        return root
    except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
        raise ConfigurationError("repo_path must be an existing directory") from None


@contextmanager
def locked_ledger(root: Path, directory: Path, *, create: bool) -> Iterator[None]:
    """Serialize initialization and record writes across processes.

    Only the checked parent may be created before taking the OS lock. Ignore-rule
    replacement runs under that lock. Empty lock files need no initial write,
    avoiding Windows buffered writes to another process's locked byte.
    """
    with _PROCESS_LOCK:
        if create:
            _prepare_ledger_parent(root)
        else:
            validate_existing_ledger(root, directory)
        handle = _open_lock(root, root / ".kuma" / "requests.lock")
        try:
            _lock(handle)
            if create:
                ensure_ledger_directory(root, directory)
            yield
        finally:
            with suppress(OSError):
                _unlock(handle)
            with suppress(OSError):
                handle.close()


def _prepare_ledger_parent(root: Path) -> None:
    """Create only a checked runtime parent before cross-process locking.

    A concurrent mkdir is harmless. Links, reparse points and different devices
    fail closed; no ignore-file replacement occurs until locked_ledger owns the
    shared OS lease. Native diagnostics are replaced by the ledger error.
    """
    failed = False
    try:
        parent = root / ".kuma"
        parent.mkdir(exist_ok=True)
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_dev != root.stat().st_dev
            or parent.resolve(strict=True) != parent
        ):
            raise OSError
    except (OSError, RuntimeError):
        failed = True
    if failed:
        raise ProviderError(
            "The request ledger is unavailable", code="operation_state_unavailable"
        ) from None


def ensure_ledger_directory(root: Path, directory: Path) -> None:
    """Create and verify ``.kuma/requests`` without crossing links or mounts."""
    try:
        ensure_repo_runtime_directory(root)
        directory.mkdir(exist_ok=True)
        validate_existing_ledger(root, directory)
    except OSError:
        raise ProviderError(
            "The request ledger is unavailable", code="operation_state_unavailable"
        ) from None


def validate_existing_ledger(root: Path, directory: Path) -> None:
    """Reject symlink, reparse, mount, and non-directory ledger components."""
    if not directory.exists():
        raise ProviderError("Request ledger was not found", code="request_not_found")
    try:
        root_device = root.stat().st_dev
        for path in (root / ".kuma", directory):
            metadata = path.lstat()
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or (reparse and getattr(metadata, "st_file_attributes", 0) & reparse)
                or metadata.st_dev != root_device
                or path.resolve(strict=True) != path
            ):
                raise OSError
    except (OSError, RuntimeError):
        raise ProviderError(
            "The request ledger is unsafe", code="operation_state_unavailable"
        ) from None


def record_paths(directory: Path) -> tuple[Path, ...]:
    """Return a bounded deterministic set of exact request record paths."""
    try:
        paths = tuple(sorted(directory.glob("kreq_*.json"), key=lambda item: item.name))
    except OSError:
        raise ProviderError(
            "The request ledger is unreadable", code="request_state_invalid"
        ) from None
    if len(paths) > MAX_RECORDS:
        raise LimitExceededError(
            "The request ledger exceeds its local record limit",
            code="client_resource_limit",
        )
    return paths


def atomic_write(
    root: Path, destination: Path, encoded: bytes, *, message: str
) -> None:
    """Atomically replace a file after descriptor and parent identity checks."""
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        temporary = Path(name)
        frozen = os.fstat(descriptor)
        named = temporary.lstat()
        if (
            not stat.S_ISREG(frozen.st_mode)
            or (frozen.st_dev, frozen.st_ino) != (named.st_dev, named.st_ino)
            or frozen.st_dev != root.stat().st_dev
        ):
            raise OSError
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        if destination.parent.name == "requests":
            validate_existing_ledger(root, destination.parent)
        else:
            ensure_safe_subdirectory(root, destination.parent)
        current = temporary.lstat()
        if (current.st_dev, current.st_ino) != (frozen.st_dev, frozen.st_ino):
            raise OSError
        os.replace(temporary, destination)
        return
    except BaseException as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        if not isinstance(exc, OSError):
            raise
    raise ProviderError(message, code="operation_state_unavailable") from None


def save_public_report(root: Path, run_id: str, value: Mapping[str, Any]) -> str:
    """Serialize and atomically save one bounded normalized public report."""
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError):
        raise ProviderError(
            "Public report is invalid", code="invalid_response"
        ) from None
    if len(encoded) > MAX_PUBLIC_REPORT_BYTES:
        raise LimitExceededError(
            "The public report exceeds its local size limit",
            code="response_too_large",
        )
    reports = root / ".kuma" / "reports"
    locator = f".kuma/reports/{run_id}.json"
    ensure_safe_subdirectory(root, reports)
    atomic_write(
        root,
        root / locator,
        encoded,
        message="The public report could not be saved",
    )
    return locator


def ensure_safe_subdirectory(root: Path, directory: Path) -> None:
    """Create one direct runtime child and reject links, reparse, or mounts."""
    try:
        directory.mkdir(exist_ok=True)
        metadata = directory.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (reparse and getattr(metadata, "st_file_attributes", 0) & reparse)
            or metadata.st_dev != root.stat().st_dev
            or directory.resolve(strict=True) != directory
        ):
            raise OSError
    except (OSError, RuntimeError):
        raise ProviderError(
            "The public report directory is unsafe",
            code="operation_state_unavailable",
        ) from None


def _open_lock(root: Path, path: Path) -> BinaryIO:
    """Open and identity-check the shared request-ledger lock file."""
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        frozen = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(frozen.st_mode)
            or (frozen.st_dev, frozen.st_ino) != (named.st_dev, named.st_ino)
            or frozen.st_dev != root.stat().st_dev
        ):
            raise OSError
        handle = os.fdopen(descriptor, "r+b")
        descriptor = None
        # Both flock and Windows byte-range locking support an empty lock file.
        # Do not initialize byte zero here: another process may already own it.
        return handle
    except OSError:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise ProviderError(
            "The request ledger lock is unavailable",
            code="operation_state_unavailable",
        ) from None


def _lock(handle: BinaryIO) -> None:
    """Acquire the platform-exclusive lock for one transaction."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle: BinaryIO) -> None:
    """Release the platform request-ledger lock."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
