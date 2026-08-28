"""Configuration resolution for the v4 SDK.

This module deliberately performs no environment or filesystem reads at import
time. Callers opt into those effects through ``resolve_api_key`` or ``configure``.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .errors import AuthenticationError, ConfigurationError

MAX_RETRIES = 5
DEFAULT_OPERATION_WAIT_TIMEOUT = 600.0
_MAX_API_KEY_BYTES = 512


def validate_max_retries(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_RETRIES
    ):
        raise ConfigurationError(f"max_retries must be between zero and {MAX_RETRIES}")
    return value


def validate_operation_wait_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationError(
            "operation_wait_timeout must be a positive finite number"
        )
    return float(value)


@dataclass(frozen=True, slots=True)
class CreateRunConfig:
    strategy: str = "auto"
    max_inputs: int | None = None
    judge: bool = True
    on_failure: str = "continue"
    allow_local: bool = False
    track_files: bool = True
    upload_diff: bool = False
    save_local: bool = False
    allow_sensitive: bool = False
    timeout: float = 300.0
    operation_wait_timeout: float = DEFAULT_OPERATION_WAIT_TIMEOUT
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, str) or not self.strategy.strip():
            raise ConfigurationError("strategy must be a non-empty string")
        if self.max_inputs is not None and (
            isinstance(self.max_inputs, bool)
            or not isinstance(self.max_inputs, int)
            or self.max_inputs <= 0
        ):
            raise ConfigurationError("max_inputs must be a positive integer")
        if not isinstance(self.on_failure, str) or self.on_failure not in {
            "continue",
            "stop",
        }:
            raise ConfigurationError("on_failure must be 'continue' or 'stop'")
        for name in (
            "judge",
            "allow_local",
            "track_files",
            "upload_diff",
            "save_local",
            "allow_sensitive",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ConfigurationError(f"{name} must be a boolean")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, int | float)
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ConfigurationError("timeout must be a positive finite number")
        validate_operation_wait_timeout(self.operation_wait_timeout)
        validate_max_retries(self.max_retries)


_CREATE_RUN_FIELDS = frozenset(CreateRunConfig.__dataclass_fields__)


def resolve_create_run_config(
    explicit: Mapping[str, object] | None = None,
    process: Mapping[str, object] | None = None,
) -> CreateRunConfig:
    """Merge defaults, process configuration, and per-call values.

    Only keys present in each mapping participate, preserving the v4 precedence
    rule: explicit call values > process configuration > SDK defaults.
    """

    merged: dict[str, object] = {}
    for source in (process or {}, explicit or {}):
        unknown = set(source) - _CREATE_RUN_FIELDS
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ConfigurationError(f"Unknown create_run configuration: {names}")
        merged.update(source)
    return CreateRunConfig(**merged)  # type: ignore[arg-type]


def credential_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the user-level credential path without creating it."""

    env = os.environ if environ is None else environ
    override = env.get("KUMA_CONFIG_HOME")
    if override:
        return Path(override).expanduser() / "credentials.json"
    if os.name == "nt" and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "KUMA" / "credentials.json"
    configured_home = env.get("XDG_CONFIG_HOME")
    if configured_home:
        base = Path(configured_home)
    else:
        try:
            base = Path.home() / ".config"
        except RuntimeError as exc:
            raise ConfigurationError(
                "Credential location unavailable; set KUMA_API_KEY explicitly"
            ) from exc
    return base / "kuma" / "credentials.json"


def validate_api_key(api_key: str) -> str:
    if not isinstance(api_key, str) or not api_key.startswith("dfx_"):
        raise ConfigurationError("KUMA API keys must begin with 'dfx_'")
    try:
        encoded = api_key.encode("ascii")
    except UnicodeEncodeError:
        raise ConfigurationError("KUMA API key format is invalid") from None
    if (
        len(encoded) <= 4
        or len(encoded) > _MAX_API_KEY_BYTES
        or any(byte < 0x21 or byte > 0x7E for byte in encoded)
    ):
        raise ConfigurationError("KUMA API key format is invalid")
    return api_key


def write_api_key(api_key: str, *, path: Path | None = None) -> Path:
    """Atomically store a validated API key in the user credential file."""

    key = validate_api_key(api_key)
    destination = (path or credential_path()).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".credentials-",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"api_key": key}, handle)
            handle.write("\n")
        with suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(destination)
        with suppress(OSError):
            destination.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def read_stored_api_key(*, path: Path | None = None) -> str | None:
    source = path or credential_path()
    if not source.is_file():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "The KUMA credential file is unreadable or invalid"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("api_key"), str):
        raise ConfigurationError("The KUMA credential file is invalid")
    return validate_api_key(payload["api_key"])


def resolve_api_key(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stored_path: Path | None = None,
    required: bool = True,
) -> str | None:
    """Resolve explicit, environment, then user-stored credentials."""

    env = os.environ if environ is None else environ
    candidate = (
        explicit or env.get("KUMA_API_KEY") or read_stored_api_key(path=stored_path)
    )
    if candidate is None:
        if required:
            raise AuthenticationError(
                "Set KUMA_API_KEY, pass api_key, or call kuma.configure()."
            )
        return None
    return validate_api_key(candidate)


__all__ = [
    "CreateRunConfig",
    "credential_path",
    "read_stored_api_key",
    "resolve_api_key",
    "resolve_create_run_config",
    "validate_api_key",
    "write_api_key",
]
