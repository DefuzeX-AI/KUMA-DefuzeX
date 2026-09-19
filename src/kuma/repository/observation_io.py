"""Explicit safe publication of already redacted local observation exports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError
from ..evidence.tracking._log_paths import resolve_log_path
from ._case_file_directory import pinned_case_directory
from .case_artifact_io import _publish


def save_observation(
    data: dict[str, Any], path: str | os.PathLike[str], *, root: str | os.PathLike[str]
) -> Path:
    """Publish a finalized session using the established pinned-directory writer.

    Session.save supplies a detached bounded export; root/path authorize only one
    existing directory tree. No mkdir, overwrite, follow-link or fallback writes.
    Map local failures outside the exception handler to remove raw OS chains.
    The shared writer owns descriptor cleanup and atomic no-replace publication.
    """
    failed = False
    try:
        supplied_root = Path(root).absolute()
        if str(supplied_root).startswith("\\\\") or supplied_root.is_symlink():
            raise ValueError("unsafe observation root")
        canonical = supplied_root.resolve(strict=True)
        destination, _, rejected = resolve_log_path(
            canonical, path, index=0, root_alias=supplied_root
        )
        if not canonical.is_dir() or rejected or destination is None:
            raise ValueError("unsafe observation destination")
        encoded = json.dumps(
            data,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        with pinned_case_directory(canonical, destination.parent) as descriptor:
            _publish(canonical, destination, encoded, descriptor)
    except (OSError, ValueError, TypeError, RuntimeError):
        failed = True
    if failed:
        raise ConfigurationError(
            "Observation could not be saved; choose a new safe path"
        )
    return destination
