"""Minimal repository metadata allowed across the public SDK boundary."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import SensitiveDataError, ValidationError
from .privacy import scan_sensitive_path

REPO_META_SCHEMA_VERSION = "1"
_SHA256_PREFIX = "sha256:"
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".kuma",
        ".git",
        ".hg",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
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
class RepoTreeEntry:
    path: str
    type: str
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        _validate_relative_path(self.path)
        if self.type not in {"file", "directory"}:
            raise ValidationError("Repo Meta entry type must be file or directory")
        if self.type == "file":
            if (
                isinstance(self.size_bytes, bool)
                or not isinstance(self.size_bytes, int)
                or self.size_bytes < 0
            ):
                raise ValidationError(
                    "Repo Meta file size_bytes must be a non-negative integer"
                )
        elif self.size_bytes is not None:
            raise ValidationError(
                "Repo Meta directory entries cannot include size_bytes"
            )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"path": self.path, "type": self.type}
        if self.size_bytes is not None:
            result["size_bytes"] = self.size_bytes
        return result


@dataclass(frozen=True, slots=True)
class RepoMeta:
    """Backend-compatible metadata with no source, README, Git, or host paths."""

    repo_fingerprint: str
    tree: tuple[RepoTreeEntry, ...]
    truncated: bool = False
    omitted_sensitive_paths: int = 0
    schema_version: str = REPO_META_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REPO_META_SCHEMA_VERSION:
            raise ValidationError("Unsupported Repo Meta schema version")
        _normalize_fingerprint(self.repo_fingerprint)
        if any(not isinstance(item, RepoTreeEntry) for item in self.tree):
            raise ValidationError("Repo Meta tree must contain RepoTreeEntry values")
        if not isinstance(self.truncated, bool):
            raise ValidationError("Repo Meta truncated must be a boolean")
        if (
            isinstance(self.omitted_sensitive_paths, bool)
            or not isinstance(self.omitted_sensitive_paths, int)
            or self.omitted_sensitive_paths < 0
        ):
            raise ValidationError(
                "Repo Meta omitted_sensitive_paths must be a non-negative integer"
            )
        object.__setattr__(self, "tree", tuple(self.tree))

    @property
    def fingerprint(self) -> str:
        """Compatibility alias for callers that used the pre-boundary name."""

        return f"{_SHA256_PREFIX}{_normalize_fingerprint(self.repo_fingerprint)}"

    def to_dict(self) -> dict[str, Any]:
        extension: dict[str, Any] = {}
        if self.truncated:
            extension["truncated"] = True
        if self.omitted_sensitive_paths:
            extension["omitted_sensitive_paths"] = self.omitted_sensitive_paths
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "repo_fingerprint": _normalize_fingerprint(self.repo_fingerprint),
            "tree": [item.to_dict() for item in self.tree],
        }
        if extension:
            result["extension"] = extension
        return result


def _normalize_fingerprint(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("repo_fingerprint must be a SHA-256 digest")
    normalized = value.removeprefix(_SHA256_PREFIX).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValidationError("repo_fingerprint must be a SHA-256 digest")
    return normalized


def _validate_relative_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\x00" in value
        or "\\" in value
    ):
        raise ValidationError("Repo Meta paths must be non-empty POSIX paths")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or value in {".", ".."} or ".." in parsed.parts:
        raise ValidationError("Repo Meta paths must stay inside the repository")
    return value


def _entry_from_mapping(value: Any) -> RepoTreeEntry:
    if not isinstance(value, Mapping):
        raise ValidationError("Repo Meta tree entries must be mappings")
    if set(value) - {"path", "type", "size_bytes"}:
        raise ValidationError("Repo Meta tree entries contain unsupported fields")
    return RepoTreeEntry(
        path=_validate_relative_path(value.get("path")),
        type=value.get("type"),
        size_bytes=value.get("size_bytes"),
    )


def prepare_repo_meta_upload(value: Mapping[str, Any] | RepoMeta) -> dict[str, Any]:
    """Validate and reduce arbitrary Repo Meta to the public Backend allowlist."""

    raw = value.to_dict() if isinstance(value, RepoMeta) else dict(value)
    if raw.get("schema_version") != REPO_META_SCHEMA_VERSION:
        raise ValidationError("Unsupported Repo Meta schema version")
    fingerprint = _normalize_fingerprint(raw.get("repo_fingerprint"))
    raw_tree = raw.get("tree")
    if not isinstance(raw_tree, list | tuple):
        raise ValidationError("Repo Meta tree must be an array")
    entries = tuple(_entry_from_mapping(item) for item in raw_tree)
    paths = [item.path for item in entries]
    if len(paths) != len(set(paths)):
        raise ValidationError("Repo Meta paths must be unique")
    sensitive = [
        item.path
        for item in entries
        if scan_sensitive_path(item.path, location="repo_meta_path")
    ]
    if sensitive:
        raise SensitiveDataError(
            "Sensitive repository paths cannot be uploaded in Repo Meta",
            details={"count": len(sensitive)},
        )
    result: dict[str, Any] = {
        "schema_version": REPO_META_SCHEMA_VERSION,
        "repo_fingerprint": fingerprint,
        "tree": [item.to_dict() for item in entries],
    }
    extension = raw.get("extension")
    if extension is not None:
        if not isinstance(extension, Mapping):
            raise ValidationError("Repo Meta extension must be a mapping")
        allowed_extension = {
            key: extension[key]
            for key in ("truncated", "omitted_sensitive_paths")
            if key in extension
        }
        if "truncated" in allowed_extension and not isinstance(
            allowed_extension["truncated"], bool
        ):
            raise ValidationError("Repo Meta extension.truncated must be a boolean")
        omitted = allowed_extension.get("omitted_sensitive_paths")
        if omitted is not None and (
            isinstance(omitted, bool) or not isinstance(omitted, int) or omitted < 0
        ):
            raise ValidationError(
                "Repo Meta extension.omitted_sensitive_paths must be non-negative"
            )
        if allowed_extension:
            result["extension"] = allowed_extension
    json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return result


def _scan_entry(
    entry: os.DirEntry[str], repo_path: Path
) -> tuple[RepoTreeEntry | None, Path | None, bool]:
    relative = Path(entry.path).relative_to(repo_path).as_posix()
    if scan_sensitive_path(relative, location="repo_meta_path"):
        return None, None, True
    try:
        if entry.is_symlink():
            return None, None, False
        if entry.is_dir(follow_symlinks=False):
            if entry.name in _EXCLUDED_DIRECTORIES:
                return None, None, False
            return RepoTreeEntry(relative, "directory"), Path(entry.path), False
        if entry.is_file(follow_symlinks=False):
            return RepoTreeEntry(relative, "file", entry.stat().st_size), None, False
    except OSError as exc:
        raise ValidationError(
            "Repository metadata could not inspect an entry", code="config_invalid"
        ) from exc
    return None, None, False


def _scan_tree(
    repo_path: Path, max_entries: int
) -> tuple[list[RepoTreeEntry], bool, int]:
    tree: list[RepoTreeEntry] = []
    pending = [repo_path]
    omitted_sensitive_paths = 0
    while pending and len(tree) < max_entries:
        current = pending.pop()
        try:
            entries = sorted(
                os.scandir(current), key=lambda entry: entry.name.casefold()
            )
        except OSError as exc:
            raise ValidationError(
                "Repository metadata could not read a directory",
                code="config_invalid",
            ) from exc
        child_directories: list[Path] = []
        for entry in entries:
            if len(tree) >= max_entries:
                return tree, True, omitted_sensitive_paths
            item, child, sensitive = _scan_entry(entry, repo_path)
            if sensitive:
                omitted_sensitive_paths += 1
            if item is not None:
                tree.append(item)
            if child is not None:
                child_directories.append(child)
        pending.extend(reversed(child_directories))
    return tree, bool(pending), omitted_sensitive_paths


def collect_repo_meta(
    repo_path: str | os.PathLike[str],
    *,
    max_entries: int = 2_000,
) -> RepoMeta:
    """Collect names, entry types, and file sizes without reading file contents."""

    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise ValidationError(
            "repo_path must be a readable directory", code="config_invalid"
        )
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries <= 0
    ):
        raise ValidationError(
            "Repo Meta limits must be positive", code="config_invalid"
        )
    try:
        next(root.iterdir(), None)
    except OSError as exc:
        raise ValidationError(
            "repo_path must be readable", code="config_invalid"
        ) from exc

    tree, truncated, omitted_sensitive_paths = _scan_tree(root, max_entries)
    fingerprint_source = {
        "schema_version": REPO_META_SCHEMA_VERSION,
        "tree": [item.to_dict() for item in tree],
        "extension": {
            "truncated": truncated,
            "omitted_sensitive_paths": omitted_sensitive_paths,
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return RepoMeta(
        repo_fingerprint=fingerprint,
        tree=tuple(tree),
        truncated=truncated,
        omitted_sensitive_paths=omitted_sensitive_paths,
    )


__all__ = [
    "REPO_META_SCHEMA_VERSION",
    "RepoMeta",
    "RepoTreeEntry",
    "collect_repo_meta",
    "prepare_repo_meta_upload",
]
