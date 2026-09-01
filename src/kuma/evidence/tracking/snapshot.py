"""Bounded, non-following filesystem snapshots for one Input step."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from types import MappingProxyType

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".kuma",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "target",
        "venv",
    }
)


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """Record one bounded, non-followed filesystem entry observation.

    Attributes:
        path: Canonical repository-relative path used for deterministic ordering.
        file_type: Observed ``file``, ``directory``, ``symlink``, or ``special``.
        mode: Platform mode bits at capture time.
        size: Observed byte size.
        mtime_ns: Modification timestamp in nanoseconds.
        device: Filesystem device identifier used for mount-boundary checks.
        inode: Filesystem entry identifier.
        sha256: Content/link digest when hashing completed, otherwise ``None``.
        hash_complete: Whether the full permitted content was hashed.
        scan_error: Stable reason for partial observation, never raw OS text.
        text_content: Bounded safe text retained only for local diff generation.
    """

    path: str
    file_type: str
    mode: int
    size: int
    mtime_ns: int
    device: int
    inode: int
    sha256: str | None
    hash_complete: bool
    scan_error: str | None = None
    text_content: str | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Freeze one deterministic view of the allowed repository root.

    Attributes:
        root: Canonical root that bounds every entry.
        entries: Read-only mapping from relative paths to observations.
        errors: Stable scan-level degradation reasons.
        complete: Whether entry count, mount, path, and read limits allowed a
            complete bounded scan.
    """

    root: Path
    entries: Mapping[str, SnapshotEntry]
    errors: tuple[str, ...] = ()
    complete: bool = True

    def __post_init__(self) -> None:
        """Freeze Snapshot entries and reasons for deterministic comparison."""
        object.__setattr__(self, "root", self.root.resolve())
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))
        object.__setattr__(self, "errors", tuple(self.errors))


def _canonical(path: Path) -> str:
    """Resolve a path without requiring the target to exist."""
    return Path(os.path.abspath(path)).as_posix()


def _is_within(path: Path, root: Path) -> bool:
    """Return whether a resolved path remains under the allowed root."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class Snapshotter:
    """Capture a bounded repository tree without following boundary escapes.

    Symlinks are recorded as symlinks and not traversed. Descendant mount/device
    changes, excluded runtime roots, filesystem roots, and paths outside the
    canonical root are rejected or recorded as partial rather than scanned.
    """

    def __init__(
        self,
        root: Path,
        *,
        excluded_roots: tuple[Path, ...] = (),
        max_entries: int = 100_000,
        max_hash_bytes: int = 64 * 1024 * 1024,
        max_text_bytes: int = 1024 * 1024,
        max_total_text_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        """Bind scanning to one canonical root, exclusions, and resource budget."""
        expanded_root = root.expanduser()
        if expanded_root.is_symlink():
            raise ValueError("snapshot root must not be a symbolic link")
        self.root = expanded_root.resolve()
        self.max_entries = max_entries
        self.max_hash_bytes = max_hash_bytes
        self.max_text_bytes = max_text_bytes
        self.max_total_text_bytes = max_total_text_bytes
        self.excluded_roots = tuple(
            path.expanduser().resolve() for path in excluded_roots
        )
        if not self.root.is_dir():
            raise ValueError("snapshot root must be an existing directory")
        if self.root == Path(self.root.anchor):
            raise ValueError("snapshot root must not be a filesystem root")
        self._root_device = self.root.lstat().st_dev
        limits = (max_entries, max_hash_bytes, max_text_bytes, max_total_text_bytes)
        if any(
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
            for limit in limits
        ):
            raise ValueError("snapshot limits must be positive integers")

    def capture(self) -> Snapshot:
        """Capture a bounded snapshot without following unsafe filesystem targets."""
        entries: dict[str, SnapshotEntry] = {}
        errors: list[str] = []
        pending = [self.root]
        truncated = False
        retained_text_bytes = 0
        text_limit_hit = False
        while pending:
            directory = pending.pop()
            if self._excluded(directory):
                continue
            children, directory_truncated, scan_error = self._scan_directory(
                directory, self.max_entries - len(entries)
            )
            if scan_error is not None:
                errors.append(scan_error)
                continue
            (
                child_directories,
                child_errors,
                retained_text_bytes,
                text_dropped,
            ) = self._capture_children(children, entries, retained_text_bytes)
            errors.extend(child_errors)
            text_limit_hit |= text_dropped
            if directory_truncated:
                if "entry_limit_exceeded" not in errors:
                    errors.append("entry_limit_exceeded")
                truncated = True
                pending.clear()
            else:
                pending.extend(reversed(child_directories))
        if text_limit_hit:
            errors.append("text_size_limit")
        return Snapshot(
            root=self.root,
            entries=entries,
            errors=tuple(errors),
            complete=not errors and not truncated,
        )

    @staticmethod
    def _scan_directory(
        directory: Path, remaining: int
    ) -> tuple[list[os.DirEntry[str]], bool, str | None]:
        """Scan one verified directory without crossing mounts or following symlinks."""
        try:
            with os.scandir(directory) as iterator:
                scanned = list(islice(iterator, remaining + 1))
        except OSError:
            return [], False, f"scan_failed:{_canonical(directory)}"
        truncated = len(scanned) > remaining
        children = sorted(scanned[:remaining], key=lambda item: item.name.casefold())
        return children, truncated, None

    def _capture_children(
        self,
        children: list[os.DirEntry[str]],
        entries: dict[str, SnapshotEntry],
        retained_text_bytes: int,
    ) -> tuple[list[Path], list[str], int, bool]:
        """Capture sorted direct children while enforcing entry and byte budgets."""
        child_directories: list[Path] = []
        errors: list[str] = []
        text_limit_hit = False
        for child in children:
            path = Path(child.path)
            if not _is_within(Path(os.path.abspath(path)), self.root):
                errors.append(f"path_outside_root:{_canonical(path)}")
                continue
            if self._excluded(path):
                continue
            entry, error = self._capture_entry(path)
            if entry is not None:
                entry, retained_text_bytes, text_dropped = self._budget_text(
                    entry, retained_text_bytes
                )
                text_limit_hit |= text_dropped
                entries[entry.path] = entry
                if entry.file_type == "directory" and error is None:
                    child_directories.append(path)
            if error is not None:
                errors.append(error)
        return child_directories, errors, retained_text_bytes, text_limit_hit

    def _budget_text(
        self, entry: SnapshotEntry, retained_bytes: int
    ) -> tuple[SnapshotEntry, int, bool]:
        """Return the stable reason for whichever Snapshot budget was exhausted."""
        if entry.text_content is None:
            return entry, retained_bytes, False
        if retained_bytes + entry.size <= self.max_total_text_bytes:
            return entry, retained_bytes + entry.size, False
        return replace(entry, text_content=None), retained_bytes, True

    def _excluded(self, path: Path) -> bool:
        """Return whether a canonical path is inside an explicitly excluded root."""
        resolved = Path(os.path.abspath(path))
        if any(
            resolved == root or _is_within(resolved, root)
            for root in self.excluded_roots
        ):
            return True
        return path != self.root and path.name.casefold() in _EXCLUDED_DIRECTORY_NAMES

    def _capture_entry(self, path: Path) -> tuple[SnapshotEntry | None, str | None]:
        """Classify one entry without following links or leaving the repository device."""
        canonical = _canonical(path)
        try:
            before = path.lstat()
        except OSError:
            return None, f"stat_failed:{canonical}"
        common = {
            "path": canonical,
            "mode": stat.S_IMODE(before.st_mode),
            "size": before.st_size,
            "mtime_ns": before.st_mtime_ns,
            "device": before.st_dev,
            "inode": before.st_ino,
        }
        if before.st_dev != self._root_device:
            return SnapshotEntry(
                **common,
                file_type="directory" if stat.S_ISDIR(before.st_mode) else "special",
                sha256=None,
                hash_complete=False,
                scan_error="mount_boundary",
            ), f"mount_boundary:{canonical}"
        if stat.S_ISLNK(before.st_mode):
            return self._capture_symlink(path, canonical, common)
        if stat.S_ISDIR(before.st_mode):
            return SnapshotEntry(
                **common,
                file_type="directory",
                sha256=None,
                hash_complete=True,
            ), None
        if not stat.S_ISREG(before.st_mode):
            return SnapshotEntry(
                **common,
                file_type="special",
                sha256=None,
                hash_complete=False,
                scan_error="special_file",
            ), f"special_file:{canonical}"
        if before.st_size > self.max_hash_bytes:
            return SnapshotEntry(
                **common,
                file_type="file",
                sha256=None,
                hash_complete=False,
                scan_error="hash_size_limit",
            ), f"hash_size_limit:{canonical}"

        return self._capture_file(path, canonical, before, common)

    @staticmethod
    def _capture_symlink(
        path: Path, canonical: str, common: Mapping[str, int | str]
    ) -> tuple[SnapshotEntry, str | None]:
        """Record symlink metadata without opening or following its target."""
        try:
            target = os.readlink(path)
        except OSError:
            return SnapshotEntry(
                **common,
                file_type="symlink",
                sha256=None,
                hash_complete=False,
                scan_error="readlink_failed",
            ), f"readlink_failed:{canonical}"
        digest = (
            "sha256:"
            + hashlib.sha256(target.encode("utf-8", errors="surrogatepass")).hexdigest()
        )
        return SnapshotEntry(
            **common,
            file_type="symlink",
            sha256=digest,
            hash_complete=True,
        ), None

    def _capture_file(
        self,
        path: Path,
        canonical: str,
        before: os.stat_result,
        common: Mapping[str, int | str],
    ) -> tuple[SnapshotEntry, str | None]:
        """Hash and optionally retain bounded UTF-8 text for one regular file."""
        digest = hashlib.sha256()
        captured = bytearray()
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    if before.st_size <= self.max_text_bytes:
                        captured.extend(chunk)
            after = path.lstat()
        except OSError:
            return SnapshotEntry(
                **common,
                file_type="file",
                sha256=None,
                hash_complete=False,
                scan_error="read_failed",
            ), f"read_failed:{canonical}"
        stable = (
            before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and before.st_dev == after.st_dev
            and before.st_ino == after.st_ino
        )
        text_content: str | None = None
        if stable and before.st_size <= self.max_text_bytes and b"\0" not in captured:
            with suppress(UnicodeDecodeError):
                text_content = bytes(captured).decode("utf-8")
        error = None if stable else "changed_during_scan"
        return SnapshotEntry(
            **common,
            file_type="file",
            sha256="sha256:" + digest.hexdigest(),
            hash_complete=stable,
            scan_error=error,
            text_content=text_content,
        ), None if stable else f"changed_during_scan:{canonical}"


__all__ = ["Snapshot", "SnapshotEntry", "Snapshotter"]
