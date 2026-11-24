"""Bounded durable local captures using existing external storage and atomic file boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import observation_contract
from .errors import ConfigurationError, ValidationError
from .evidence.tracking._log_paths import open_verified_log
from .repository._case_file_directory import pinned_case_directory
from .repository.case_artifact_io import _publish, _unique_object
from .repository.observation_io import save_observation
from .repository.privacy import scan_sensitive_json
from .storage import resolve_storage_path
from .trajectory_capture import classify_observation
from .trajectory_requirements import validate_requirements

MAX_CAPTURE_BYTES = 6 * 1024 * 1024
CAPTURE_FIELDS = {
    "schema_version",
    "capture_id",
    "requirements",
    "observation",
    "instrumentation",
}


def _local_path(value: Any) -> Path:
    """Reject explicit network/device path syntax before any filesystem lookup."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or raw.startswith(("\\\\", "//")):
        raise ValueError
    return Path(raw).expanduser()


def capture_id(value: Any) -> str:
    """Accept only an opaque local capture identity, never a filename, URL or traversal."""
    if not isinstance(value, str) or re.fullmatch(r"cap_[0-9a-f]{32}", value) is None:
        raise ValidationError("Invalid capture ID")
    return value


def encoded(value: Any) -> bytes:
    """Encode already validated finite JSON consistently without changing integer precision."""
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def validate_capture(value: Any) -> dict:
    """Validate the persisted binding without inferring task success or restoring omitted data."""
    if (
        not isinstance(value, dict)
        or set(value) != CAPTURE_FIELDS
        or value["schema_version"] != "kuma.capture.v1"
    ):
        raise ValidationError("Invalid local capture document")
    capture_id(value["capture_id"])
    validate_requirements(value["requirements"])
    observation_contract.checked(observation_contract.export, value["observation"])
    integration = value["instrumentation"]
    if (
        not isinstance(integration, dict)
        or set(integration) != {"framework", "status"}
        or not isinstance(integration["framework"], str)
        or integration["framework"]
        not in {"existing", "openai", "langchain", "langgraph"}
        or not isinstance(integration["status"], str)
        or integration["status"]
        not in {
            "application_owned",
            "kuma_reused",
            "external_binding_unverified",
            "kuma_enabled",
        }
    ):
        raise ValidationError("Invalid capture integration metadata")
    raw = encoded(value)
    if len(raw) > MAX_CAPTURE_BYTES or scan_sensitive_json(value, location="capture"):
        raise ValidationError("Capture exceeds its local size or privacy boundary")
    return json.loads(raw)


def _mkdir(root: Path, parent: Path, name: str) -> Path:
    """Create one SDK-owned directory through a pinned, no-follow parent boundary."""
    with pinned_case_directory(root, parent) as descriptor:
        try:
            if descriptor is None:
                (parent / name).mkdir(mode=0o700)
            else:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
        except FileExistsError:
            pass  # The next pinned boundary verifies that an existing entry is a safe directory.
    child = parent / name
    with pinned_case_directory(root, child):
        return child


def capture_directory(
    repo_path: str | os.PathLike = ".",
    storage_path: str | os.PathLike | None = None,
    *,
    create: bool = False,
) -> Path:
    """Resolve an external capture directory; only opt-in capture creates directories.

    repo_path is an existing workspace. Explicit storage_path uses the existing
    disjoint external-storage contract; None selects ~/.kuma-captures, outside the
    workspace. Files live under .kuma/captures. Reads never mkdir or fall back.
    Invalid/link/mount/permission failures produce safe ConfigurationError text.
    """
    try:
        repo = _local_path(repo_path).resolve(strict=True)
        if not repo.is_dir():
            raise ValueError
        selected = storage_path
        if selected is None:
            selected = _local_path(Path.home() / ".kuma-captures")
            candidate = selected.resolve()
            if (
                candidate == repo
                or candidate in repo.parents
                or repo in candidate.parents
            ):
                raise ValueError
            if create:
                selected.mkdir(mode=0o700, exist_ok=True)
        root = resolve_storage_path(repo, _local_path(selected))
        if create:
            parent = _mkdir(root, root, ".kuma")
            return _mkdir(root, parent, "captures")
        directory = root / ".kuma" / "captures"
        with pinned_case_directory(root, directory):
            return directory
    except (OSError, ValueError, TypeError, RuntimeError, ConfigurationError):
        pass  # Safe boundary translation; no fallback to workspace storage.
    raise ConfigurationError(
        "Capture storage is unavailable; choose a safe existing external storage_path"
    )


def save_capture(directory: Path, document: dict) -> Path:
    """Atomically publish one closed validated capture without replacing previous data."""
    safe = validate_capture(document)
    return save_observation(
        safe, directory / (safe["capture_id"] + ".json"), root=directory
    )


def _read(directory: Path, path: Path, maximum: int) -> bytes:
    """Read a bounded regular no-follow handle; native file errors never escape."""
    try:
        with open_verified_log(directory, path)[0] as stream:
            return stream.read(maximum + 1)
    except OSError:
        pass  # Suppress raw path/OS error chains, not the observable read failure.
    raise ConfigurationError("Local capture file is unavailable")


def load_capture(
    identifier: str,
    *,
    repo_path: str | os.PathLike = ".",
    storage_path: str | os.PathLike | None = None,
) -> tuple[dict, Path]:
    """Read exactly one bounded, no-follow local capture; no network, keys or writes."""
    identifier = capture_id(identifier)
    directory = capture_directory(repo_path, storage_path)
    failed = False
    try:
        raw = _read(directory, directory / (identifier + ".json"), MAX_CAPTURE_BYTES)
        if len(raw) > MAX_CAPTURE_BYTES:
            raise ValueError
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError, RecursionError):
        failed = True
    if failed:
        raise ConfigurationError("Capture is missing, unreadable or invalid")
    safe = validate_capture(data)
    if safe["capture_id"] != identifier:
        raise ValidationError("Capture identity does not match its local file")
    return safe, directory


def list_captures(
    *,
    repo_path: str | os.PathLike = ".",
    storage_path: str | os.PathLike | None = None,
    limit: int = 20,
) -> list[dict]:
    """Read at most limit summaries, newest local file first; never emit task/body content.

    Args:
        repo_path: Existing original workspace path, default current directory; no scanning.
        storage_path: Original external parent or None for ~/.kuma-captures; must exist.
        limit: Integer 1..100, default 20; maximum summaries, not automatic pagination.
    Returns:
        List of metadata dicts sorted by file modification time then name, newest first.
        Each has capture_id, execution_status, capture_status, span_count, operation_counts
        and reasons. Counts describe retained observations, not inferred Agent activity.
    Raises:
        ValidationError: Invalid limit or invalid stored capture content.
        ConfigurationError: Absent/unreadable/unsafe store or file; no fake complete entry.
    Side Effects:
        Bounded no-follow local file reads with handles closed on completion/failure.
        Does not create a store, read credentials, contact a service or output task bodies.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("Capture list limit must be an integer from 1 to 100")
    directory = capture_directory(repo_path, storage_path)
    try:
        paths = [
            p
            for p in directory.iterdir()
            if re.fullmatch(r"cap_[0-9a-f]{32}\.json", p.name)
        ]
        paths.sort(key=lambda p: (p.lstat().st_mtime_ns, p.name), reverse=True)
    except OSError:
        raise ConfigurationError("Capture summaries are unavailable") from None
    result = []
    for path in paths[:limit]:
        document, _ = load_capture(
            path.stem, repo_path=repo_path, storage_path=storage_path
        )
        observation = document["observation"]
        result.append(
            {
                "capture_id": path.stem,
                "execution_status": observation["execution_status"],
                "capture_status": observation["capture_status"],
                "span_count": len(observation["spans"]),
                "operation_counts": classify_observation(observation)["counts"],
                "reasons": observation["reasons"],
            }
        )
    return result


@contextmanager
def output_target(path: str | os.PathLike) -> Iterator[Any]:
    """Preflight a new writable target, then publish one complete JSON file atomically.

    The parent must exist. Pin it and verify a write/fsync/no-replace probe before
    yielding a publish callback. A later collision never overwrites another file;
    callers must retain any paid result independently before invoking the callback.
    This does not promise prevention of later disk faults. No automatic mkdir.
    """
    try:
        destination = _local_path(path).absolute()
        if destination.parent.is_symlink():
            raise ValueError
        parent = destination.parent.resolve(strict=True)
        destination = parent / destination.name
        with pinned_case_directory(parent, parent) as descriptor:
            if destination.exists() or destination.is_symlink():
                raise ValueError
            probe = parent / (".kuma-capture-probe-" + uuid.uuid4().hex)
            _publish(parent, probe, b"{}", descriptor)
            if descriptor is None:
                probe.unlink()
            else:
                os.unlink(probe.name, dir_fd=descriptor)

            def publish(value: dict) -> None:
                """Atomically publish validated result bytes without replacing another file."""
                _publish(parent, destination, encoded(value), descriptor)

            yield publish
            return
    except (OSError, ValueError, TypeError, RuntimeError):
        pass  # A safe failure is raised after pinned handles have closed.
    raise ConfigurationError(
        "Output unavailable; reuse the same capture/key with a new safe destination"
    )


def save_review(directory: Path, review: dict) -> str:
    """Keep a safe paid/stopped response before optional output publication; no replacement."""
    raw = encoded(review)
    if len(raw) > 8 * 1024 * 1024 or scan_sensitive_json(
        review, location="capture_review"
    ):
        raise ValidationError("Review exceeds its local privacy or size boundary")
    digest = hashlib.sha256(raw).hexdigest()
    path = directory / ("review_" + digest + ".json")
    if path.exists():
        if _read(directory, path, len(raw)) != raw:
            raise ConfigurationError("Stored review content does not match")
    else:
        try:
            save_observation(review, path, root=directory)
        except ConfigurationError:
            if not path.exists():
                raise
            if _read(directory, path, len(raw)) != raw:
                raise ConfigurationError(
                    "Concurrent stored review content does not match"
                ) from None
    return digest


def preflight_capture_write(directory: Path) -> None:
    """Prove current result-store write permission before paid work; later faults can still occur."""
    with output_target(directory / ("review-preflight-" + uuid.uuid4().hex + ".json")):
        pass  # output_target already wrote/fsynced/removed its exclusive local probe.
