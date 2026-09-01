"""Safe local paths and atomic repository runtime-directory setup."""

from __future__ import annotations

import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from .errors import ConfigurationError


def default_lock_path() -> Path:
    """Return the shared platform lock path for the single-Run invariant."""
    runtime_root = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_root:
        return Path(runtime_root).expanduser() / "kuma" / "active-run.lock"
    if os.name == "posix":
        return Path("/tmp/kuma-active-run.lock")
    return Path(tempfile.gettempdir()) / "kuma-active-run.lock"


def default_runtime_root(mode: str) -> Path:
    """Return the SDK-owned root used for temporary Run resources."""
    if mode == "docker":
        return Path("/tmp/kuma")
    return Path(tempfile.gettempdir()) / "kuma"


def _create_repo_runtime_directory(path: Path) -> bool:
    """Create ``path`` or verify a concurrently/existing directory.

    Returns ``True`` only when this call created the directory, allowing its
    caller to remove an empty partial setup after a later atomic-write failure.
    """
    try:
        path.mkdir()
        return True
    except FileExistsError:
        if path.is_dir():
            return False
        raise


def _write_runtime_ignore_rule(root: Path, gitignore: Path, existing: str) -> None:
    """Atomically append the KUMA runtime ignore rule and clean failed temps.

    Existing newline style and file mode are retained. The helper deliberately
    re-raises filesystem and encoding errors for the owning public boundary to
    translate into one safe :class:`ConfigurationError`.
    """
    separator = "\r\n" if "\r\n" in existing else "\n"
    updated = existing
    if updated and not updated.endswith(("\n", "\r")):
        updated += separator
    updated += f"/.kuma/{separator}"
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".gitignore.kuma-",
            suffix=".tmp",
            dir=root,
            text=True,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = None
            handle.write(updated)
        if gitignore.exists():
            temporary.chmod(stat.S_IMODE(gitignore.stat().st_mode))
        temporary.replace(gitignore)
    except BaseException:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise


def _remove_empty_runtime_directory(path: Path, *, created: bool) -> None:
    """Remove only an empty runtime directory created by the failed operation."""
    if created:
        with suppress(OSError):
            path.rmdir()


def ensure_repo_runtime_directory(repo_path: Path) -> Path:
    """Create the repository runtime directory and idempotent ignore rule.

    Args:
        repo_path: Existing authorized repository root.

    Returns:
        Absolute ``<repo>/.kuma`` directory path.

    Raises:
        ConfigurationError: If the repository is missing, its ``.gitignore`` is
            not readable UTF-8 text, or atomic runtime setup cannot complete.

    Postconditions:
        ``.kuma`` exists and ``/.kuma/`` is represented once by an equivalent
        ignore rule. A failed update removes its temporary file and any new empty
        ``.kuma`` directory.

    Side Effects:
        May create ``.kuma`` and atomically replace ``.gitignore``. No repository
        files are enumerated or uploaded.
    """
    created_runtime_directory = False
    runtime_directory: Path | None = None
    failure_message: str | None = None
    try:
        root = repo_path.expanduser().resolve()
        if not root.is_dir():
            raise ConfigurationError("repo_path must be an existing directory")
        runtime_directory = root / ".kuma"
        created_runtime_directory = _create_repo_runtime_directory(runtime_directory)

        gitignore = root / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        normalized_rules = {line.strip() for line in existing.splitlines()}
        if ".kuma/" in normalized_rules or "/.kuma/" in normalized_rules:
            return runtime_directory

        _write_runtime_ignore_rule(root, gitignore, existing)
        return runtime_directory
    except UnicodeError:
        assert runtime_directory is not None
        _remove_empty_runtime_directory(
            runtime_directory, created=created_runtime_directory
        )
        failure_message = "Repository .gitignore must be readable UTF-8 text"
    except ConfigurationError:
        raise
    except (AttributeError, TypeError, ValueError, OSError):
        if runtime_directory is not None:
            _remove_empty_runtime_directory(
                runtime_directory, created=created_runtime_directory
            )
        failure_message = "Repository runtime storage is unavailable"
    raise ConfigurationError(failure_message) from None


__all__ = [
    "default_lock_path",
    "default_runtime_root",
    "ensure_repo_runtime_directory",
]
