"""Explicit runtime checks, container-wide locking, and Run directories."""

from __future__ import annotations

import json
import os
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Literal

from ._runtime_filesystem import (
    default_lock_path,
    default_runtime_root,
    ensure_repo_runtime_directory,
)
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
    """Detect whether SDK and Agent appear to share a Docker container.

    Args:
        marker_paths: Container marker files to inspect; defaults to
            ``/.dockerenv``. Tests may provide isolated paths.
        cgroup_path: Linux cgroup file checked for Docker/containerd markers when
            no marker file exists.

    Returns:
        ``True`` when a marker or cgroup token identifies Docker, otherwise
        ``False``. Unreadable cgroup metadata is treated as not detected.

    Side Effects:
        Reads only the supplied marker existence and cgroup text.
    """

    if any(path.exists() for path in marker_paths):
        return True
    try:
        cgroup = cgroup_path.read_text(encoding="utf-8", errors="replace").casefold()
    except OSError:
        return False
    return any(token in cgroup for token in _DOCKER_CGROUP_TOKENS)


def resolve_runtime_mode(*, allow_local: bool) -> RuntimeMode:
    """Choose the supported Docker mode or an explicit trusted local mode.

    Args:
        allow_local: Explicit development opt-in when Docker is not detected.

    Returns:
        ``"docker"`` when detected, otherwise ``"local"`` only when opted in.

    Raises:
        DockerRequiredError: If Docker is absent and local execution was not
            explicitly permitted.

    Security/Privacy:
        ``allow_local=True`` does not create isolation or relax Evidence/path
        validation; it only acknowledges that the caller trusts local execution.
    """
    if is_running_in_docker():
        return "docker"
    if allow_local:
        return "local"
    raise DockerRequiredError(
        "KUMA runs must start inside the same Docker container as the Agent; "
        "use allow_local=True only for development tests."
    )


def _lock_file(handle: BinaryIO) -> None:
    """Acquire the platform file lock without blocking another active Run."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    """Release a previously acquired platform file lock."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _resolve_lock_path(path: Path | None) -> Path:
    """Resolve the shared lock location without retaining raw path failures.

    Args:
        path: Explicit lock path, or ``None`` for the platform-wide default.

    Returns:
        Canonical absolute lock path used by :class:`ContainerRunLock`.

    Raises:
        ConfigurationError: If the path cannot be converted or resolved. The
            original filesystem exception is detached from the public chain.
    """
    failed = False
    try:
        resolved = (path or default_lock_path()).expanduser().resolve()
    except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
        failed = True
    if failed:
        raise ConfigurationError("KUMA runtime lock is unavailable") from None
    return resolved


def _open_lock_handle(path: Path) -> BinaryIO:
    """Open and initialize the lock byte, closing partial ownership on failure.

    Args:
        path: Canonical lock-file path owned by the runtime boundary.

    Returns:
        Open binary handle ready for a non-blocking OS lock.

    Raises:
        ConfigurationError: If parent creation, opening, seeking, or initial
            writing fails. Any opened handle is closed before the safe error.

    Side Effects:
        May create the parent and lock file; never leaves a caller-owned handle
        after failure.
    """
    handle: BinaryIO | None = None
    failed = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
    except OSError:
        if handle is not None:
            with suppress(OSError):
                handle.close()
        failed = True
    if failed:
        raise ConfigurationError("KUMA runtime lock is unavailable") from None
    assert handle is not None
    return handle


def _claim_lock_handle(handle: BinaryIO) -> None:
    """Claim one opened lock handle or close it and report stable contention.

    Args:
        handle: Open initialized handle returned by :func:`_open_lock_handle`.

    Raises:
        RunAlreadyActiveError: If the platform refuses the non-blocking lock.
            The raw OSError and lock path are detached from the public chain.

    Postconditions:
        Success leaves the handle locked; failure closes it.
    """
    failed = False
    try:
        _lock_file(handle)
    except OSError:
        with suppress(OSError):
            handle.close()
        failed = True
    if failed:
        raise RunAlreadyActiveError(
            "Another KUMA Run is already active in this container"
        ) from None


def _write_lock_metadata(handle: BinaryIO, run_id: str) -> None:
    """Durably write public lease metadata and release ownership on failure.

    Args:
        handle: Currently locked runtime handle.
        run_id: Validated public identifier written with PID and start time.

    Raises:
        ConfigurationError: If metadata cannot be written and fsynced; the lock
            is released, the handle closed, and the raw failure detached.
        BaseException: Non-filesystem control-flow failures after releasing the
            same resources.

    Postconditions:
        Success leaves the lock owned for :class:`ContainerRunLock`; failure
        transfers no handle ownership to its caller.
    """
    failed = False
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
    except BaseException as exc:
        with suppress(OSError):
            _unlock_file(handle)
        with suppress(OSError):
            handle.close()
        if isinstance(exc, OSError):
            failed = True
        else:
            raise
    if failed:
        raise ConfigurationError("KUMA runtime lock is unavailable") from None


class ContainerRunLock:
    """One active Run per container, enforced by the operating system."""

    def __init__(self, path: Path | None = None) -> None:
        """Bind the single-Run lease to a validated local lock location.

        Raises:
            ConfigurationError: If the selected path is invalid or cannot be
                resolved. The fixed error does not expose host path details.
        """
        self.path = _resolve_lock_path(path)
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        """Return whether this lock currently owns the process-wide Run lease."""
        return self._handle is not None

    def acquire(self, run_id: str) -> None:
        """Acquire the single-active-Run lease without waiting.

        Args:
            run_id: Safe public identifier recorded as non-secret lock metadata.

        Raises:
            ConfigurationError: If ``run_id`` is unsafe for local state.
            RunAlreadyActiveError: If this object or another process/container
                already owns the shared lock.
            ConfigurationError: If the lock file or its public metadata cannot
                be created and written safely.

        Preconditions:
            No Run has been acquired through this lock instance.

        Postconditions:
            Success retains an open OS-locked handle until :meth:`release` and
            writes run ID, PID, and UTC start time. Metadata-write failure unlocks
            and closes the handle before re-raising.

        Side Effects:
            Creates the lock parent/file when needed and fsyncs public lock
            metadata. It never stores credentials or Case/Evidence content.
        """
        _validate_run_id(run_id)
        if self._handle is not None:
            raise RunAlreadyActiveError("This lock instance already owns an active Run")
        handle = _open_lock_handle(self.path)
        _claim_lock_handle(handle)
        _write_lock_metadata(handle, run_id)
        self._handle = handle

    def release(self) -> None:
        """Release the Run lease idempotently and close its lock handle.

        Raises:
            ConfigurationError: If the operating system cannot release or close
                the lock. Internal path and OS diagnostics are never exposed.
        """
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        release_failed = False
        try:
            _unlock_file(handle)
            handle.close()
        except OSError:
            with suppress(OSError):
                handle.close()
            release_failed = True
        if release_failed:
            raise ConfigurationError("KUMA runtime lock is unavailable") from None

    def __enter__(self) -> ContainerRunLock:
        """Acquire or expose the managed resource for a context block."""
        if not self.acquired:
            raise RuntimeError("Call acquire() before entering ContainerRunLock")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the managed resource when leaving a context block."""
        self.release()


def _validate_run_id(run_id: str) -> None:
    """Reject Run identifiers unsafe for lock names or owned directory paths."""
    if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ConfigurationError("run_id contains unsupported path characters")


def _resolve_workspace_roots(
    repo_path: Path, mode: RuntimeMode, runtime_root: Path | None
) -> tuple[Path, Path]:
    """Resolve repository/runtime roots and create only the controlled parent.

    Args:
        repo_path: Existing repository authorized for this Run.
        mode: Runtime mode used only to select the default temporary root.
        runtime_root: Explicit controlled parent, or ``None`` for the default.

    Returns:
        Canonical repository root and canonical temporary-runtime root.

    Raises:
        ConfigurationError: If either path is invalid/unavailable or the runtime
            parent cannot be created, without retaining raw filesystem details.
    """
    failed = False
    try:
        root = repo_path.expanduser().resolve()
        if not root.is_dir():
            raise ConfigurationError("repo_path must be an existing directory")
        resolved_runtime_root = (
            (runtime_root or default_runtime_root(mode)).expanduser().resolve()
        )
        resolved_runtime_root.mkdir(parents=True, exist_ok=True)
    except ConfigurationError:
        raise
    except (AttributeError, TypeError, ValueError, OSError):
        failed = True
    if failed:
        raise ConfigurationError("KUMA runtime workspace is unavailable") from None
    return root, resolved_runtime_root


def _create_temporary_workspace(runtime_root: Path, run_id: str) -> Path:
    """Create the exclusive Run child and hide collision path diagnostics.

    Args:
        runtime_root: Canonical SDK-controlled temporary parent.
        run_id: Validated public Run identifier used as the child name.

    Returns:
        Newly created exclusive Run directory.

    Raises:
        ConfigurationError: On a collision or local creation failure. Neither
            the absolute root nor the original OSError remains in the chain.
    """
    temporary_path = runtime_root / run_id
    failure_message: str | None = None
    try:
        temporary_path.mkdir()
    except FileExistsError:
        failure_message = f"Runtime directory already exists for {run_id}"
    except OSError:
        failure_message = "KUMA runtime workspace is unavailable"
    if failure_message is not None:
        raise ConfigurationError(failure_message) from None
    return temporary_path


def _create_persistent_workspace(
    persistent_path: Path | None, temporary_path: Path, run_id: str
) -> None:
    """Create optional local Evidence storage or roll back the temporary Run.

    Args:
        persistent_path: Run-owned output directory, or ``None`` when disabled.
        temporary_path: Temporary directory to remove if persistence fails.
        run_id: Validated public identifier used for a stable collision message.

    Raises:
        ConfigurationError: If persistent storage collides or cannot be created;
            raw host diagnostics are detached.

    Postconditions:
        Success creates the requested directory. Failure best-effort removes the
        temporary workspace before returning the stable error.
    """
    if persistent_path is None:
        return
    failure_message: str | None = None
    try:
        persistent_path.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        with suppress(OSError):
            shutil.rmtree(temporary_path)
        failure_message = (
            f"Runtime directory already exists for {run_id}"
            if isinstance(exc, FileExistsError)
            else "KUMA runtime workspace is unavailable"
        )
    if failure_message is not None:
        raise ConfigurationError(failure_message) from None


@dataclass(slots=True)
class RuntimeWorkspace:
    """Own temporary and optional persistent directories for one Run.

    Attributes:
        run_id: Validated public Run identifier used in directory names.
        repo_path: Canonical authorized repository root.
        temporary_path: Random Run-owned directory removed on close.
        persistent_path: Stable ``.kuma/runs/<run_id>`` directory when local
            persistence is enabled, otherwise ``None``.
        runtime_root: Canonical parent allowed to contain temporary directories.
        _closed: Whether cleanup already ran; makes ``close`` idempotent.
    """

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
        """Create temporary and optional persistent directories for one Run.

        Args:
            run_id: Safe identifier used as the exact child directory name.
            repo_path: Existing authorized repository root.
            mode: Resolved ``docker`` or ``local`` mode.
            save_local: Create persistent ``.kuma/runs/<run_id>`` output when true.
            runtime_root: Optional controlled temporary parent for tests/embedding.

        Returns:
            Open workspace whose temporary directory is uniquely owned by this Run.

        Raises:
            ConfigurationError: If identifiers/paths are invalid or a same-name
                runtime directory already exists, or directory creation fails.

        Postconditions:
            Failure after temporary creation removes that temporary directory.
            Success leaves cleanup ownership with the returned workspace.

        Side Effects:
            Creates runtime directories and may update repository ``.gitignore``.
        """
        _validate_run_id(run_id)
        root, resolved_runtime_root = _resolve_workspace_roots(
            repo_path, mode, runtime_root
        )
        temporary_path = _create_temporary_workspace(resolved_runtime_root, run_id)
        try:
            repo_runtime = ensure_repo_runtime_directory(root)
        except BaseException:
            with suppress(OSError):
                shutil.rmtree(temporary_path)
            raise
        persistent_path = repo_runtime / "runs" / run_id if save_local else None
        _create_persistent_workspace(persistent_path, temporary_path, run_id)
        return cls(
            run_id=run_id,
            repo_path=root,
            temporary_path=temporary_path,
            persistent_path=persistent_path,
            runtime_root=resolved_runtime_root,
        )

    def close(self) -> None:
        """Remove only this Run's validated temporary workspace, idempotently.

        Raises:
            ConfigurationError: If the cleanup target is not the exact expected
                child of ``runtime_root`` or safe deletion fails.

        Postconditions:
            On success the temporary path no longer exists. Persistent local
            Evidence remains available. Repeated successful calls do nothing.
            A failed deletion remains retryable instead of being marked closed.

        Side Effects:
            Recursively removes the exact Run-owned temporary directory only.
        """
        if self._closed:
            return
        cleanup_failed = False
        try:
            resolved = self.temporary_path.resolve()
            if (
                resolved.parent != self.runtime_root.resolve()
                or resolved.name != self.run_id
            ):
                raise ConfigurationError(
                    "Refusing to clean an unexpected runtime directory"
                )
            if resolved.exists():
                shutil.rmtree(resolved)
        except ConfigurationError:
            raise
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            raise ConfigurationError("KUMA runtime workspace is unavailable") from None
        self._closed = True


@dataclass(slots=True)
class RuntimeSession:
    """Own the active-Run lease and workspace as one rollback-safe resource.

    Attributes:
        run_id: Public Run identifier associated with both resources.
        mode: ``docker`` or explicitly permitted ``local`` runtime mode.
        lock: Process/container-wide active-Run lease.
        workspace: Run-owned temporary/persistent filesystem paths.
        _closed: Whether both resources have already been released.
    """

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
        """Acquire the active-Run lease and create its workspace atomically.

        Args:
            run_id: Safe public Run identifier.
            repo_path: Existing authorized repository root.
            allow_local: Explicit permission to use trusted local mode.
            save_local: Whether committed Evidence receives a persistent directory.
            lock_path: Optional isolated lock path for tests/embedding.
            runtime_root: Optional isolated temporary root.

        Returns:
            Open session owning both lock and workspace.

        Raises:
            DockerRequiredError: Docker is absent without local opt-in.
            RunAlreadyActiveError: Another Run owns the lease.
            ConfigurationError: Identifier or repository/runtime path is invalid,
                unavailable, or cannot be created safely.

        Postconditions:
            Success owns both resources. Workspace failure releases the acquired
            lock before re-raising, so no leaked active-Run state remains.

        Side Effects:
            Acquires an OS lock and creates the Run directories.
        """
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
        """Close workspace and release the active-Run lease exactly once.

        Postconditions:
            The lock is released even if workspace cleanup raises. Repeated calls
            after success are no-ops; a cleanup failure may be retried safely.

        Side Effects:
            Removes validated temporary files and closes the shared lock handle.
        """
        if self._closed:
            return
        try:
            self.workspace.close()
        finally:
            self.lock.release()
        self._closed = True

    def __enter__(self) -> RuntimeSession:
        """Acquire or expose the managed resource for a context block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the managed resource when leaving a context block."""
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
