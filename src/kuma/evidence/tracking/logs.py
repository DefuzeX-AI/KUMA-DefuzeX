"""Transactional, per-file log increment capture."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...contracts import CaptureComponent
from ._log_paths import open_verified_log, resolve_log_path

_ALLOWED_SUFFIXES = frozenset({".txt", ".log", ".json", ".jsonl", ".md"})


@dataclass(frozen=True, slots=True)
class LogState:
    """Remember the last committed read position for one canonical log file.

    Attributes:
        device: Filesystem device identifier used to detect replacement.
        inode: File identifier used to detect replacement/rotation.
        offset: Next byte offset to capture after the previous commit.
        last_size: File size at the previous committed observation.
        prefix_sha256: Digest of the stable prefix used to detect rewrite/truncate.
        segment_no: Monotonic committed segment number for this file.
    """

    device: int
    inode: int
    offset: int
    last_size: int
    prefix_sha256: str
    segment_no: int


@dataclass(frozen=True, slots=True)
class _CapturedLog:
    """Represent one attempted log read before transaction assembly.

    Attributes:
        segment: Safe bounded log mapping when capture succeeds.
        state: Next offset state to install only after Submission commit.
        reason: Stable omission/degradation code when no complete segment exists.
    """

    segment: Mapping[str, Any] | None = None
    state: LogState | None = None
    reason: str | None = None


@dataclass(slots=True)
class PreparedLogs:
    """Stage bounded log segments and offsets until Submission commit.

    Attributes:
        segments: Ordered safe log artifacts prepared for Evidence.
        component: Public log capture completeness.
        missing: Stable missing/degradation reasons.
        dropped_count: Number of log files/segments omitted.
        _tracker: Log tracker that owns committed offsets.
        _next_states: Canonical-path offsets to install on commit.
        _finished: Whether commit or abort already finalized the preparation.
    """

    segments: tuple[Mapping[str, Any], ...]
    component: CaptureComponent
    missing: tuple[str, ...]
    dropped_count: int
    _tracker: LogTracker
    _next_states: Mapping[str, LogState]
    _finished: bool = False

    def commit(self) -> None:
        """Commit prepared capture state only after the enclosing submission succeeds."""
        if self._finished:
            return
        self._tracker._commit(self._next_states)
        self._finished = True

    def abort(self) -> None:
        """Discard prepared capture state after a failed enclosing submission."""
        self._finished = True


class LogTracker:
    """Capture bounded incremental log bytes without advancing on failed submit.

    The tracker accepts explicit paths from ``Run.submit(logs=...)`` only after
    ``EvidenceCollector`` supplies the allowed repository/runtime root. It stages
    new offsets in :class:`PreparedLogs`; repeated preparation reads the same
    bytes until a successful Submission commits them.
    """

    def __init__(
        self,
        *,
        root: Path,
        max_segment_bytes: int = 10 * 1024 * 1024,
        max_files: int = 20,
        max_total_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        """Initialize incremental log accounting with hard resource limits.

        Args:
            root: Canonical repository directory that exclusively bounds every
                relative or absolute log path accepted by this tracker.
            max_segment_bytes: Maximum new bytes retained from one file per step.
            max_files: Maximum explicit log paths inspected per Submission.
            max_total_bytes: Maximum combined new bytes retained per Submission.

        Raises:
            ValueError: If ``root`` is a symlink/non-directory or any limit is
                boolean, non-integer, zero, or negative.

        Postconditions:
            ``root`` is canonical, no path is registered, and no file is read.
        """
        expanded_root = Path(root).expanduser()
        if expanded_root.is_symlink():
            raise ValueError("log capture root must not be a symlink")
        self._root_alias = Path(os.path.abspath(expanded_root))
        self.root = expanded_root.resolve()
        if not self.root.is_dir():
            raise ValueError("log capture root must be a directory")
        limits = (max_segment_bytes, max_files, max_total_bytes)
        if any(
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
            for limit in limits
        ):
            raise ValueError("log capture limits must be positive integers")
        self.max_segment_bytes = max_segment_bytes
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self._states: dict[str, LogState] = {}

    def prepare(self, paths: Sequence[str | os.PathLike[str]] | None) -> PreparedLogs:
        """Stage bounded incremental reads from logs under the configured root.

        ``EvidenceCollector`` supplies user-selected paths. This method rejects
        symlinks and paths outside the allowed root, reads only bytes after each
        committed offset, and returns ``PreparedLogs``. Its ``commit()`` advances
        tracker offsets only after the owning Submission commits; ``abort()``
        leaves offsets unchanged so a retry observes the same bytes.

        Args:
            paths: Explicit ordered log-file paths, or ``None`` for skipped
                capture. Relative paths resolve from ``root``. At most
                ``max_files`` are inspected; additional paths are counted.

        Returns:
            Transactional :class:`PreparedLogs` containing safe structured
            segments, completeness, omissions, and staged next offsets.

        Preconditions:
            ``EvidenceCollector`` supplied the same canonical repository root.
            Callers must not treat this as arbitrary file access.

        Postconditions:
            Committed offsets remain unchanged until ``PreparedLogs.commit``.
            Rejected paths become stable missing reasons, not raw exceptions.

        Side Effects:
            Reads bounded suffix bytes and file identity/stat metadata. It writes
            nothing and does not follow symlinks.

        Security/Privacy:
            Paths outside the root, unsupported suffixes, symlinks/reparse
            points, binary/invalid content, and oversize data degrade using
            stable index-based reasons. Evidence contains repository-relative
            paths only; host absolute paths and OS error text are never retained.
        """
        requested = self._requested_paths(paths)
        if not requested:
            return PreparedLogs(
                segments=(),
                component=CaptureComponent(status="skipped"),
                missing=(),
                dropped_count=0,
                _tracker=self,
                _next_states=MappingProxyType({}),
            )
        segments: list[Mapping[str, Any]] = []
        missing: list[str] = []
        next_states: dict[str, LogState] = {}
        seen: set[str] = set()
        dropped = 0
        if len(requested) > self.max_files:
            requested.pop()
            missing.append(f"log_file_limit:{self.max_files}")
            dropped += 1
        captured_bytes = 0
        for index, supplied in enumerate(requested):
            remaining_bytes = self.max_total_bytes - captured_bytes
            if remaining_bytes <= 0:
                missing.append(f"log_total_size_limit:{index}")
                dropped += 1
                break
            path, canonical, display_path, rejected = self._resolve_path(
                supplied,
                index=index,
                seen=seen,
                next_states=next_states,
            )
            if rejected is not None:
                missing.append(rejected)
                dropped += 1
                continue
            if path is None or canonical is None or display_path is None:
                continue
            capture_bytes = min(self.max_segment_bytes, remaining_bytes)
            limit_reason = (
                "log_total_size_limit"
                if capture_bytes < self.max_segment_bytes
                else "log_size_limit"
            )
            captured = self._capture_log(
                path,
                canonical,
                display_path=display_path,
                index=index,
                capture_bytes=capture_bytes,
                limit_reason=limit_reason,
            )
            if captured.segment is not None:
                segments.append(captured.segment)
                captured_bytes += (
                    captured.segment["end_offset"] - captured.segment["start_offset"]
                )
            if captured.state is not None:
                next_states[canonical] = captured.state
            if captured.reason is not None:
                missing.append(captured.reason)
                dropped += 1

        status = self._capture_status(missing, segments)
        return PreparedLogs(
            segments=tuple(segments),
            component=CaptureComponent(status=status, reasons=tuple(missing)),
            missing=tuple(missing),
            dropped_count=dropped,
            _tracker=self,
            _next_states=MappingProxyType(next_states),
        )

    def _requested_paths(
        self, paths: Sequence[str | os.PathLike[str]] | None
    ) -> list[str | os.PathLike[str]]:
        """Copy at most one path beyond the file limit for bounded accounting.

        Args:
            paths: Validated ordered paths or ``None``.

        Returns:
            A bounded mutable copy used only for the current preparation.

        Raises:
            ValueError: If an internal caller bypasses ``Run.submit`` and passes
                a scalar or unordered/non-sequence container.
        """
        if paths is None:
            return []
        if isinstance(paths, (str, bytes, bytearray)) or not isinstance(
            paths, Sequence
        ):
            raise ValueError("log paths must be an ordered path sequence")
        return list(islice(iter(paths), self.max_files + 1))

    def _resolve_path(
        self,
        supplied: str | os.PathLike[str],
        *,
        index: int,
        seen: set[str],
        next_states: Mapping[str, LogState],
    ) -> tuple[Path | None, str | None, str | None, str | None]:
        """Resolve one path within ``root`` without exposing its host spelling.

        Args:
            supplied: Caller-selected text/path-like value.
            index: Zero-based position used in public-safe degradation reasons.
            seen: Canonical internal state keys already handled this preparation.
            next_states: Staged offsets used to enforce the cross-step file cap.

        Returns:
            Lexical path, private state key, repository-relative display path,
            and stable rejection reason. Rejected/duplicate paths use ``None``
            placeholders and never return caller text.

        Side Effects:
            Performs bounded ``lstat`` checks on components inside ``root`` only;
            it does not open a log or follow a link.

        Security/Privacy:
            Foreign Windows drives/UNC paths, POSIX escapes, ``..`` escapes, and
            symlink/reparse components are rejected before file content reads.
        """
        path, relative, rejected = resolve_log_path(
            self.root,
            supplied,
            index=index,
            root_alias=self._root_alias,
        )
        if rejected is not None or path is None or relative is None:
            return None, None, None, rejected

        canonical = os.path.normcase(os.path.normpath(str(path)))
        display_path = relative.as_posix()
        if canonical in seen:
            return None, None, None, None
        seen.add(canonical)
        if (
            canonical not in self._states
            and canonical not in next_states
            and len(self._states) + len(next_states) >= self.max_files
        ):
            return None, None, None, f"log_file_limit:{index}"
        if path.suffix.casefold() not in _ALLOWED_SUFFIXES:
            return None, None, None, f"unsupported_log_type:{index}"
        return path, canonical, display_path, None

    def _capture_log(
        self,
        path: Path,
        canonical: str,
        *,
        display_path: str,
        index: int,
        capture_bytes: int,
        limit_reason: str,
    ) -> _CapturedLog:
        """Read one stable bounded UTF-8 suffix and stage its next offset.

        Args:
            path: Already boundary-checked non-symlink log file.
            canonical: Stable canonical path key used for committed offset state.
            display_path: Repository-relative path safe for public Evidence.
            index: Zero-based request position used in degradation reasons.
            capture_bytes: Maximum suffix bytes to read this step.
            limit_reason: Stable reason prefix used when unread bytes remain.

        Returns:
            Captured segment/next state, or a stable value-free failure reason.

        Preconditions:
            ``_resolve_path`` accepted the path and budgets are positive.

        Postconditions:
            Tracker committed offsets are unchanged. The returned next state is
            installed only by ``PreparedLogs.commit``. Rotation/truncation starts
            a new segment rather than skipping bytes.

        Side Effects:
            Opens and reads the file plus stable stat metadata; writes nothing.

        Security/Privacy:
            Binary files and unstable reads are rejected. Evidence retains only
            ``display_path`` and index-based reasons; host paths, OS exception
            text, and arbitrary object representations are not retained.
        """
        try:
            handle, frozen = open_verified_log(self.root, path)
            with handle:
                size = frozen.st_size
                prefix_hash = hashlib.sha256(handle.read(min(size, 4096))).hexdigest()
                segment_no, start = self._segment_position(
                    canonical,
                    device=frozen.st_dev,
                    inode=frozen.st_ino,
                    size=size,
                    prefix_hash=prefix_hash,
                )
                end = min(size, start + capture_bytes)
                handle.seek(start)
                payload = handle.read(end - start)
                if len(payload) != end - start:
                    raise OSError("log changed while reading")
        except OSError:
            return _CapturedLog(reason=f"log_read_failed:{index}")

        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            return _CapturedLog(reason=f"binary_log_unsupported:{index}")

        complete = end == size
        segment = None
        if payload:
            segment = MappingProxyType(
                {
                    "path": display_path,
                    "segment_no": segment_no,
                    "start_offset": start,
                    "end_offset": end,
                    "encoding": "utf-8",
                    "binary": False,
                    "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    "complete": complete,
                    "content": content,
                }
            )
        state = LogState(
            device=frozen.st_dev,
            inode=frozen.st_ino,
            offset=end,
            last_size=size,
            prefix_sha256=prefix_hash,
            segment_no=segment_no,
        )
        reason = None if complete else f"{limit_reason}:{index}"
        return _CapturedLog(segment=segment, state=state, reason=reason)

    def _segment_position(
        self,
        canonical: str,
        *,
        device: int,
        inode: int,
        size: int,
        prefix_hash: str,
    ) -> tuple[int, int]:
        """Determine offsets while detecting truncation or file replacement."""
        previous = self._states.get(canonical)
        if previous is None:
            return 0, 0
        rotated = (previous.device, previous.inode) != (device, inode) or (
            size <= previous.last_size and prefix_hash != previous.prefix_sha256
        )
        if rotated or size < previous.offset:
            return previous.segment_no + 1, 0
        return previous.segment_no, previous.offset

    @staticmethod
    def _capture_status(missing: list[str], segments: list[Mapping[str, Any]]) -> str:
        """Summarize log capture as complete, partial, failed, or not requested."""
        if missing and not segments:
            return "failed"
        if missing:
            return "partial"
        return "complete"

    def _commit(self, states: Mapping[str, LogState]) -> None:
        """Advance file offsets only after the owning Submission commits."""
        self._states.update(states)


__all__ = ["LogState", "LogTracker", "PreparedLogs"]
