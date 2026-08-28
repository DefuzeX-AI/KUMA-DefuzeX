"""Explicit runtime checks, container-wide locking, and Run directories."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal

from .errors import ConfigurationError, DockerRequiredError, RunAlreadyActiveError

RuntimeMode = Literal["docker", "local"]
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DOCKER_MARKERS = (Path("/.dockerenv"),)
_DOCKER_CGROUP_TOKENS = ("docker", "containerd")


def is_running_in_docker(
    *,
    marker_paths: tuple[Path, ...] = _DOCKER_MARKERS,
    cgroup_path: Path = Path("/proc/1/cgroup"),
) -> bool:
    """Detect the Docker container that must contain both SDK and Agent."""

    if any(path.exists() for path in marker_paths):
        return True
    try:
        cgroup = cgroup_path.read_text(encoding="utf-8", errors="replace").casefold()
    except OSError:
        return False
    return any(token in cgroup for token in _DOCKER_CGROUP_TOKENS)


def resolve_runtime_mode(*, allow_local: bool) -> RuntimeMode:
    if is_running_in_docker():
        return "docker"
    if allow_local:
        return "local"
    raise DockerRequiredError(
        "KUMA runs must start inside the same Docker container as the Agent; "
        "use allow_local=True only for development tests."
    )


def default_lock_path() -> Path:
    runtime_root = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_root:
        return Path(runtime_root).expanduser() / "kuma" / "active-run.lock"
    if os.name == "posix":
        # Without XDG_RUNTIME_DIR, POSIX processes must resolve one fallback lock
        # to preserve the single-active-Run invariant.
        return Path("/tmp/kuma-active-run.lock")  # nosec B108
    return Path(tempfile.gettempdir()) / "kuma-active-run.lock"


def default_runtime_root(mode: RuntimeMode) -> Path:
    if mode == "docker":
        # Runs use random child names; cleanup validates each child's parent/name.
        return Path("/tmp/kuma")  # nosec B108
    return Path(tempfile.gettempdir()) / "kuma"


def _lock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ContainerRunLock:
    """One active Run per container, enforced by the operating system."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or default_lock_path()).expanduser().resolve()
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self, run_id: str) -> None:
        _validate_run_id(run_id)
        if self._handle is not None:
            raise RunAlreadyActiveError("This lock instance already owns an active Run")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            _lock_file(handle)
        except OSError as exc:
            handle.close()
            raise RunAlreadyActiveError(
                "Another KUMA Run is already active in this container"
            ) from exc
        try:
            metadata = json.dumps(
                {
                    "run_id": run_id,
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
                sort_keys=True,
            ).encode("utf-8")
            handle.seek(1)
            handle.truncate()
            handle.write(metadata)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            _unlock_file(handle)
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock_file(handle)
        finally:
            handle.close()

    def __enter__(self) -> ContainerRunLock:
        if not self.acquired:
            raise RuntimeError("Call acquire() before entering ContainerRunLock")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ConfigurationError("run_id contains unsupported path characters")


def ensure_repo_runtime_directory(repo_path: Path) -> Path:
    """Create the excluded .kuma directory and idempotent Git ignore rule."""

    root = repo_path.expanduser().resolve()
    if not root.is_dir():
        raise ConfigurationError("repo_path must be an existing directory")
    runtime_directory = root / ".kuma"
    runtime_directory.mkdir(exist_ok=True)

    gitignore = root / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(
            "Repository .gitignore must be readable UTF-8 text"
        ) from exc
    normalized_rules = {line.strip() for line in existing.splitlines()}
    if ".kuma/" in normalized_rules or "/.kuma/" in normalized_rules:
        return runtime_directory

    separator = "\r\n" if "\r\n" in existing else "\n"
    updated = existing
    if updated and not updated.endswith(("\n", "\r")):
        updated += separator
    updated += f"/.kuma/{separator}"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".gitignore.kuma-",
        suffix=".tmp",
        dir=root,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        if gitignore.exists():
            temporary.chmod(stat.S_IMODE(gitignore.stat().st_mode))
        temporary.replace(gitignore)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return runtime_directory


@dataclass(slots=True)
class RuntimeWorkspace:
    run_id: str
    repo_path: Path
    temporary_path: Path
    persistent_path: Path | None
    runtime_root: Path
    _closed: bool = False

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        repo_path: Path,
        mode: RuntimeMode,
        save_local: bool,
        runtime_root: Path | None = None,
    ) -> RuntimeWorkspace:
        _validate_run_id(run_id)
        root = repo_path.expanduser().resolve()
        repo_runtime = ensure_repo_runtime_directory(root)
        resolved_runtime_root = (
            (runtime_root or default_runtime_root(mode)).expanduser().resolve()
        )
        resolved_runtime_root.mkdir(parents=True, exist_ok=True)
        temporary_path = resolved_runtime_root / run_id
        try:
            temporary_path.mkdir()
        except FileExistsError as exc:
            raise ConfigurationError(
                f"Runtime directory already exists for {run_id}"
            ) from exc
        persistent_path = repo_runtime / "runs" / run_id if save_local else None
        try:
            if persistent_path is not None:
                persistent_path.mkdir(parents=True, exist_ok=False)
        except BaseException:
            shutil.rmtree(temporary_path)
            raise
        return cls(
            run_id=run_id,
            repo_path=root,
            temporary_path=temporary_path,
            persistent_path=persistent_path,
            runtime_root=resolved_runtime_root,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        resolved = self.temporary_path.resolve()
        if (
            resolved.parent != self.runtime_root.resolve()
            or resolved.name != self.run_id
        ):
            raise RuntimeError("Refusing to clean an unexpected runtime directory")
        if resolved.exists():
            shutil.rmtree(resolved)


@dataclass(slots=True)
class RuntimeSession:
    run_id: str
    mode: RuntimeMode
    lock: ContainerRunLock
    workspace: RuntimeWorkspace
    _closed: bool = False

    @classmethod
    def open(
        cls,
        *,
        run_id: str,
        repo_path: Path,
        allow_local: bool,
        save_local: bool,
        lock_path: Path | None = None,
        runtime_root: Path | None = None,
    ) -> RuntimeSession:
        mode = resolve_runtime_mode(allow_local=allow_local)
        lock = ContainerRunLock(lock_path)
        lock.acquire(run_id)
        try:
            workspace = RuntimeWorkspace.create(
                run_id=run_id,
                repo_path=repo_path,
                mode=mode,
                save_local=save_local,
                runtime_root=runtime_root,
            )
        except BaseException:
            lock.release()
            raise
        return cls(run_id=run_id, mode=mode, lock=lock, workspace=workspace)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.workspace.close()
        finally:
            self.lock.release()

    def __enter__(self) -> RuntimeSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "ContainerRunLock",
    "RuntimeMode",
    "RuntimeSession",
    "RuntimeWorkspace",
    "default_lock_path",
    "default_runtime_root",
    "ensure_repo_runtime_directory",
    "is_running_in_docker",
    "resolve_runtime_mode",
]
