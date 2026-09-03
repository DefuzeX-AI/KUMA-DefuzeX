"""Bounded local file I/O for the Agent tool capability contract."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import SensitiveDataError, ValidationError
from .privacy import enforce_sensitive_policy, scan_sensitive_path
from .tool_capabilities import (
    MAX_CAPABILITY_FILE_BYTES,
    AgentCapabilities,
    _canonical_bytes,
    _closed_mapping,
    scan_agent_tools,
    validate_agent_capabilities,
)


def _read_json_file(path: str | os.PathLike[str]) -> tuple[Path, Any]:
    """Read one explicitly selected bounded UTF-8 JSON file and nothing else."""
    source = Path(path).expanduser().resolve()
    if scan_sensitive_path(source, location="tool_capabilities"):
        raise SensitiveDataError("Credential files cannot be tool manifests")
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_CAPABILITY_FILE_BYTES + 1)
    except OSError as exc:
        raise ValidationError(
            "Tool capability file is missing or unreadable",
            code="tool_capabilities_invalid",
        ) from exc
    if len(raw) > MAX_CAPABILITY_FILE_BYTES:
        raise ValidationError(
            "Tool capability file exceeds the size limit",
            code="tool_capabilities_invalid",
        )
    try:
        return source, json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "Tool capability file must contain valid UTF-8 JSON",
            code="tool_capabilities_invalid",
        ) from exc


def scan_agent_tool_manifest(path: str | os.PathLike[str]) -> AgentCapabilities:
    """Scan one explicitly selected local JSON manifest without code discovery.

    Args:
        path: Path to a bounded UTF-8 JSON object containing the exact field
            ``tools``. It is the only source file read.

    Returns:
        Canonical scanner-generated capability document for user review.

    Raises:
        ValidationError: If the file or its closed source shape is invalid.
        SensitiveDataError: If the selected path or content looks sensitive.

    Preconditions:
        The user explicitly authorizes reading this one manifest.

    Postconditions:
        No output file is written and no Agent/tool code executes.

    Side Effects:
        Reads at most :data:`MAX_CAPABILITY_FILE_BYTES` from ``path``.

    Security/Privacy:
        The scanner neither traverses directories nor accesses the network.
    """
    _, value = _read_json_file(path)
    source = _closed_mapping(value, frozenset({"tools"}), "scanner manifest")
    return scan_agent_tools(source["tools"])


def load_agent_capabilities(path: str | os.PathLike[str]) -> AgentCapabilities:
    """Load and validate one manually selected canonical capability file.

    Args:
        path: Explicit local UTF-8 JSON file. Manual files normally declare
            ``provenance`` as ``user_declared``; scanner drafts retain
            ``scanner_generated`` even after review and edits.

    Returns:
        Immutable validated capability document. Declared tools are accepted as
        user authority and are not checked against runtime implementations.

    Raises:
        ValidationError: If reading, JSON decoding, schema, or limits fail.
        SensitiveDataError: If path/content is classified as sensitive.

    Preconditions:
        The user explicitly selected this file.

    Postconditions:
        Success changes no Run or filesystem state.

    Side Effects:
        Reads only the selected bounded file.

    Security/Privacy:
        No network access, imports, tool execution, or directory discovery occurs.
    """
    _, value = _read_json_file(path)
    return validate_agent_capabilities(value)


def save_agent_capabilities(
    document: AgentCapabilities | Mapping[str, Any],
    path: str | os.PathLike[str],
) -> Path:
    """Atomically save a reviewed canonical capability document locally.

    Args:
        document: Validated object or edited plain canonical mapping. Edited
            mappings are revalidated before any write.
        path: User-selected destination file. Its parent must already exist.

    Returns:
        Absolute destination path after atomic replacement succeeds.

    Raises:
        ValidationError: If document validation, destination, size, or write fails.
        SensitiveDataError: If content or destination resembles credential data.

    Preconditions:
        The caller has reviewed the document and authorized this exact path.

    Postconditions:
        Success leaves one complete canonical file; failure never publishes a
        partial destination and removes the temporary sibling when possible.

    Side Effects:
        Creates one temporary sibling and atomically replaces ``path``. It does
        not submit the document, change a Run, or access the network.

    Security/Privacy:
        Credentials/private fields always fail closed; ``allow_sensitive`` does
        not apply to tool capability contracts.
    """
    validated = (
        document
        if isinstance(document, AgentCapabilities)
        else validate_agent_capabilities(document)
    )
    data = _canonical_bytes(validated)
    destination = Path(path).expanduser().resolve()
    enforce_sensitive_policy(
        scan_sensitive_path(destination, location="tool_capabilities"),
        allow_sensitive=False,
    )
    if not destination.parent.is_dir():
        raise ValidationError(
            "Tool capability destination directory does not exist",
            code="tool_capabilities_invalid",
        )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ValidationError(
            "Tool capability file could not be saved",
            code="tool_capabilities_invalid",
        ) from exc
    return destination


__all__ = [
    "load_agent_capabilities",
    "save_agent_capabilities",
    "scan_agent_tool_manifest",
]
