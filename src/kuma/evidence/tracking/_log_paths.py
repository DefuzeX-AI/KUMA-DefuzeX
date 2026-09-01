"""Cross-platform repository boundary checks for explicit log paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PureWindowsPath
from typing import BinaryIO


def resolve_log_path(
    root: Path,
    supplied: str | os.PathLike[str],
    *,
    index: int,
    root_alias: Path,
) -> tuple[Path | None, Path | None, str | None]:
    """Resolve one log path without following links or exposing host spelling.

    Args:
        root: Canonical repository root owned by the current Run.
        supplied: Caller-selected text/path-like log path.
        index: Zero-based request position used in public-safe reasons.
        root_alias: Absolute spelling of ``root`` retained before
            canonicalization, such as macOS ``/var`` for ``/private/var``.

    Returns:
        Absolute unresolved path, repository-relative path, and rejection
        reason. Rejection returns ``None`` paths and an index-only reason.

    Side Effects:
        Performs bounded ``lstat`` calls only after the lexical path is proven
        inside ``root``. It never opens a file or follows a link.

    Security/Privacy:
        Foreign drives/UNC paths, lexical escapes, symlink/reparse components,
        caller spellings, and OS error text never enter the returned reason.
    """
    path, relative, rejected = _lexical_path(
        root,
        supplied,
        index=index,
        root_alias=root_alias,
    )
    if rejected is not None or path is None or relative is None:
        return None, None, rejected
    rejected = _component_rejection(root, relative, index=index)
    if rejected is not None:
        return None, None, rejected
    return path, relative, None


def _lexical_path(
    root: Path,
    supplied: str | os.PathLike[str],
    *,
    index: int,
    root_alias: Path,
) -> tuple[Path | None, Path | None, str | None]:
    """Normalize one spelling and enforce the lexical repository boundary.

    Windows drive/UNC forms are classified before ``Path`` resolution so a
    foreign share cannot trigger an accidental host/network lookup. An
    absolute path under the exact pre-canonical root spelling is translated
    lexically to ``root``; no arbitrary symlink target is resolved. The
    returned path remains unresolved for the link-component check.
    """
    try:
        raw_path = os.fspath(supplied)
        if not isinstance(raw_path, str) or "\0" in raw_path:
            raise TypeError("path must resolve to text")
        windows_path = PureWindowsPath(raw_path)
        root_windows = PureWindowsPath(str(root))
        foreign_drive = windows_path.drive and (
            not windows_path.is_absolute()
            or os.name != "nt"
            or windows_path.drive.casefold() != root_windows.drive.casefold()
        )
        foreign_windows_root = os.name != "nt" and raw_path.startswith("\\")
        if foreign_drive or foreign_windows_root:
            return None, None, f"log_path_outside_root:{index}"
        supplied_path = Path(raw_path).expanduser()
        if not supplied_path.is_absolute():
            relative = supplied_path
        else:
            absolute = Path(os.path.abspath(supplied_path))
            try:
                relative = absolute.relative_to(root)
            except ValueError:
                relative = absolute.relative_to(root_alias)
        path = Path(os.path.abspath(root / relative))
        return path, path.relative_to(root), None
    except ValueError:
        return None, None, f"log_path_outside_root:{index}"
    except (KeyError, OSError, TypeError):
        return None, None, f"invalid_log_path:{index}"


def _component_rejection(root: Path, relative: Path, *, index: int) -> str | None:
    """Return a safe reason for a link, mount boundary, or metadata failure.

    Missing components are left for the ordinary bounded read-failure path.
    Existing components are inspected without following their targets.
    """
    try:
        root_device = root.stat().st_dev
    except OSError:
        return f"invalid_log_path:{index}"
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            return f"invalid_log_path:{index}"
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(metadata.st_mode) or (
            reparse_flag and getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            return f"log_path_symlink:{index}"
        if metadata.st_dev != root_device:
            return f"log_path_mount_boundary:{index}"
    return None


def open_verified_log(root: Path, path: Path) -> tuple[BinaryIO, os.stat_result]:
    """Open one regular log and bind its handle to the canonical Run root.

    Args:
        root: Canonical repository root authorized for the current Run.
        path: Canonical lexical path previously accepted under ``root``.

    Returns:
        A binary file handle positioned at byte zero and the immutable metadata
        captured from that same handle. The caller owns and must close the handle.

    Raises:
        OSError: If opening fails, a link/reparse or mount boundary appears, the
            path leaves ``root``, or its current identity differs from the open
            handle.

    Preconditions:
        ``path`` came from :func:`resolve_log_path`; callers still must treat
        that earlier result as stale because an attacker can replace components.

    Postconditions:
        On success, no bytes have been read and the returned handle refers to the
        same regular file currently named by canonical ``path`` inside ``root``.
        On failure, any opened descriptor is closed.

    Side Effects:
        Opens one read-only descriptor and performs bounded filesystem metadata
        lookups. It does not write, follow an accepted link, or read file bytes.

    Security/Privacy:
        ``O_NOFOLLOW`` blocks a final-link swap where available. The post-open
        identity check also protects platforms without that flag and detects
        parent-component, mount, or check/open races before Evidence sees data.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        frozen = os.fstat(descriptor)
        _verify_open_descriptor(root, path, frozen)
        return os.fdopen(descriptor, "rb"), frozen
    except BaseException:
        os.close(descriptor)
        raise


def _verify_open_descriptor(
    root: Path,
    path: Path,
    frozen: os.stat_result,
) -> None:
    """Reject an opened descriptor unless its current path identity is stable.

    The comparison deliberately happens after ``os.open`` and before any read.
    It binds the descriptor identity to both ``lstat`` and the resolved target,
    while requiring the target and descriptor to remain on the Run root device.
    """
    try:
        canonical_target = path.resolve(strict=True)
        canonical_target.relative_to(root)
        named = path.lstat()
        target = canonical_target.stat()
        root_device = root.stat().st_dev
    except (OSError, RuntimeError, ValueError) as exc:
        raise OSError("log path identity is not stable") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    invalid = (
        canonical_target != path
        or not stat.S_ISREG(frozen.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or (
            bool(reparse_flag)
            and bool(getattr(named, "st_file_attributes", 0) & reparse_flag)
        )
        or (frozen.st_dev, frozen.st_ino) != (named.st_dev, named.st_ino)
        or (frozen.st_dev, frozen.st_ino) != (target.st_dev, target.st_ino)
        or frozen.st_dev != root_device
    )
    if invalid:
        raise OSError("log path identity changed during open")


__all__ = ["open_verified_log", "resolve_log_path"]
