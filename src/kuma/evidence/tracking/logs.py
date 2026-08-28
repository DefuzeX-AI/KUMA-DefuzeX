"""Transactional, per-file log increment capture."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ...contracts import CaptureComponent

_ALLOWED_SUFFIXES = frozenset({".txt", ".log", ".json", ".jsonl", ".md"})


@dataclass(frozen=True, slots=True)
class LogState:
    device: int
    inode: int
    offset: int
    last_size: int
    prefix_sha256: str
    segment_no: int


@dataclass(frozen=True, slots=True)
class _CapturedLog:
    segment: Mapping[str, Any] | None = None
    state: LogState | None = None
    reason: str | None = None


@dataclass(slots=True)
class PreparedLogs:
    segments: tuple[Mapping[str, Any], ...]
    component: CaptureComponent
    missing: tuple[str, ...]
    dropped_count: int
    _tracker: LogTracker
    _next_states: Mapping[str, LogState]
    _finished: bool = False

    def commit(self) -> None:
        if self._finished:
            return
        self._tracker._commit(self._next_states)
        self._finished = True

    def abort(self) -> None:
        self._finished = True


class LogTracker:
    def __init__(
        self,
        *,
        max_segment_bytes: int = 10 * 1024 * 1024,
        max_files: int = 20,
        max_total_bytes: int = 20 * 1024 * 1024,
    ) -> None:
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

    def prepare(self, paths: Iterable[str | os.PathLike[str]] | None) -> PreparedLogs:
        requested = (
            [] if paths is None else list(islice(iter(paths), self.max_files + 1))
        )
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
            missing.append("log_file_limit")
            dropped += 1
        captured_bytes = 0
        for supplied in requested:
            remaining_bytes = self.max_total_bytes - captured_bytes
            if remaining_bytes <= 0:
                missing.append("log_total_size_limit")
                dropped += 1
                break
            path, canonical, rejected = self._resolve_path(
                supplied, seen=seen, next_states=next_states
            )
            if rejected is not None:
                missing.append(rejected)
                dropped += 1
                continue
            if path is None or canonical is None:
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

    def _resolve_path(
        self,
        supplied: str | os.PathLike[str],
        *,
        seen: set[str],
        next_states: Mapping[str, LogState],
    ) -> tuple[Path | None, str | None, str | None]:
        try:
            path = Path(supplied).expanduser().resolve()
        except (TypeError, OSError):
            return None, None, "invalid_log_path"
        canonical = path.as_posix()
        if canonical in seen:
            return None, None, None
        seen.add(canonical)
        if (
            canonical not in self._states
            and canonical not in next_states
            and len(self._states) + len(next_states) >= self.max_files
        ):
            return None, None, f"log_file_limit:{canonical}"
        if path.suffix.casefold() not in _ALLOWED_SUFFIXES:
            return None, None, f"unsupported_log_type:{canonical}"
        return path, canonical, None

    def _capture_log(
        self,
        path: Path,
        canonical: str,
        *,
        capture_bytes: int,
        limit_reason: str,
    ) -> _CapturedLog:
        try:
            with path.open("rb") as handle:
                frozen = os.fstat(handle.fileno())
                if not stat.S_ISREG(frozen.st_mode):
                    raise OSError("not a regular file")
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
            return _CapturedLog(reason=f"log_read_failed:{canonical}")

        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            return _CapturedLog(reason=f"binary_log_unsupported:{canonical}")

        complete = end == size
        segment = None
        if payload:
            segment = MappingProxyType(
                {
                    "path": canonical,
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
        reason = None if complete else f"{limit_reason}:{canonical}"
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
        if missing and not segments:
            return "failed"
        if missing:
            return "partial"
        return "complete"

    def _commit(self, states: Mapping[str, LogState]) -> None:
        self._states.update(states)


__all__ = ["LogState", "LogTracker", "PreparedLogs"]
