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
DEFAULT_CASE_MAX_STEPS = 10
_MAX_API_KEY_BYTES = 512


def validate_max_retries(value: int) -> int:
    """Validate the number of HTTP retries allowed after the first attempt.

    Args:
        value: Integer from ``0`` through :data:`MAX_RETRIES`. ``0`` disables
            retries; it does not disable the initial request. Booleans are not
            accepted as integers.

    Returns:
        The unchanged validated retry count.

    Raises:
        ConfigurationError: If ``value`` is outside the closed range or is not
            an integer.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_RETRIES
    ):
        raise ConfigurationError(f"max_retries must be between zero and {MAX_RETRIES}")
    return value


def validate_operation_wait_timeout(value: float) -> float:
    """Validate the total wait deadline for an asynchronous Case or Judge operation.

    Args:
        value: Positive finite seconds covering all polling attempts. This is
            separate from the timeout of each individual HTTP request.

    Returns:
        The validated deadline converted to ``float`` seconds.

    Raises:
        ConfigurationError: If the value is boolean, non-numeric, non-finite,
            zero, or negative.
    """
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
    """Validated configuration used to assemble one :class:`kuma.run.Run`.

    Callers normally pass these options to :func:`kuma.create_run` rather than
    constructing this internal value directly. ``resolve_create_run_config``
    merges defaults and process configuration before this dataclass validates
    them, so later lifecycle code can rely on the invariants below.

    Attributes:
        strategy: Public Case strategy identifier. ``"auto"`` lets the official
            service choose; a non-empty explicit identifier requests that exact
            strategy.
        max_steps: Positive upper bound on Case steps, or ``None`` to use the
            selected provider/service default. It is not an exact requested
            count. Custom Case Providers require an explicit value.
        judge: Whether the Run automatically requests a final report after its
            last submission.
        on_failure: ``"continue"`` delivers the next input after a failed,
            timed-out, or aborted submission; ``"stop"`` ends the Run early.
        allow_local: Explicit opt-in to run against a local repository when no
            supported container boundary is detected. It does not weaken path
            or sensitive-data checks.
        track_files: Capture bounded file snapshots and changes for each step.
        upload_diff: Include bounded textual diffs in Evidence when safe. File
            metadata can still be captured when this is ``False``.
        save_local: Persist committed Run Evidence under the SDK-owned runtime
            directory. Prepared but uncommitted Evidence is not finalized.
        allow_sensitive: Permit sensitive values only where the relevant public
            Evidence contract explicitly allows them. It never permits secrets,
            private rubrics, or credentials in official transport.
        timeout: Positive finite timeout in seconds for one HTTP request.
        operation_wait_timeout: Positive finite total seconds spent polling one
            accepted asynchronous Case or Judge operation.
        max_retries: Number of bounded transient HTTP retries after the first
            attempt, from ``0`` through :data:`MAX_RETRIES`.
        scan_strategy_group: Explicit opt-in to conservative local Strategy
            Group selection from declared and intrinsic Evidence capabilities.

    Raises:
        ConfigurationError: During construction when any field violates its
            documented type, range, or closed-value constraint.
    """

    strategy: str = "auto"
    max_steps: int | None = None
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
    scan_strategy_group: bool = False

    def __post_init__(self) -> None:
        """Reject invalid Run options before filesystem or network effects.

        Preconditions:
            Configuration sources have been merged; values are still untrusted
            Python objects and may have incorrect runtime types.

        Postconditions:
            Success guarantees all booleans are actual ``bool`` values, timeouts
            are positive and finite, retry count is bounded, and ``max_steps``
            is either ``None`` or a positive integer.

        Raises:
            ConfigurationError: On the first invalid option.

        Side Effects:
            None. Validation does not read credentials, inspect repositories,
            create runtime directories, or perform network requests.
        """
        if not isinstance(self.strategy, str) or not self.strategy.strip():
            raise ConfigurationError("strategy must be a non-empty string")
        if self.max_steps is not None and (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps <= 0
        ):
            raise ConfigurationError("max_steps must be a positive integer")
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
            "scan_strategy_group",
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
    """Merge process-wide and per-call Run options into one validated config.

    Only keys present in each mapping participate, preserving the v4 precedence
    rule: explicit call values > process configuration > SDK defaults.

    Args:
        explicit: Options supplied directly to ``create_run``. Present keys,
            including values equal to defaults, override ``process``.
        process: Options previously registered by ``kuma.configure``. ``None``
            behaves as an empty mapping.

    Returns:
        Immutable :class:`CreateRunConfig` with defaults filled and all values
        validated.

    Raises:
        ConfigurationError: If either mapping contains an unknown key or the
            merged value violates a configuration constraint.

    Postconditions:
        Neither input mapping is mutated, and no external I/O has occurred.
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
    """Resolve the user-level credential file location without creating it.

    Args:
        environ: Environment mapping used for resolution, or ``None`` to use
            ``os.environ``. Tests and embedded callers can pass an isolated map.

    Returns:
        Platform-appropriate ``credentials.json`` path. ``KUMA_CONFIG_HOME``
        takes precedence, followed by Windows ``APPDATA`` or the XDG config
        directory and home-directory fallback.

    Raises:
        ConfigurationError: If no home/config location can be resolved.

    Security/Privacy:
        The function reads path-setting environment variables only. It neither
        reads nor creates the credential file and never returns a credential.
    """

    env = os.environ if environ is None else environ
    override = env.get("KUMA_CONFIG_HOME")
    location_failed = False
    try:
        if override:
            return Path(override).expanduser() / "credentials.json"
        if os.name == "nt" and env.get("APPDATA"):
            return Path(env["APPDATA"]) / "KUMA" / "credentials.json"
        configured_home = env.get("XDG_CONFIG_HOME")
        base = Path(configured_home) if configured_home else Path.home() / ".config"
    except (TypeError, ValueError, OSError, RuntimeError):
        location_failed = True
    if location_failed:
        raise ConfigurationError(
            "Credential location unavailable; set KUMA_API_KEY explicitly"
        ) from None
    return base / "kuma" / "credentials.json"


def validate_api_key(api_key: str) -> str:
    """Validate an opaque KUMA key before it can enter an HTTP header.

    Args:
        api_key: Printable ASCII token beginning with ``dfx_`` and occupying at
            most 512 bytes. Spaces, tabs, line breaks, NUL, and non-ASCII text
            are rejected to prevent header injection and backend disagreement.

    Returns:
        The unchanged opaque key. The SDK never parses scopes or account data
        from the token itself.

    Raises:
        ConfigurationError: If the value has an invalid prefix, type, byte
            length, or header-unsafe character.

    Security/Privacy:
        The key is not logged, normalized, truncated, or included in an error.
    """
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
    """Atomically store one validated API key in a user-selected credential file.

    Args:
        api_key: Opaque ``dfx_`` key accepted by :func:`validate_api_key`.
        path: Destination file. ``None`` uses :func:`credential_path`; parent
            directories are created when missing.

    Returns:
        Absolute path of the committed credential file.

    Raises:
        ConfigurationError: If the key is invalid or the default location is
            unavailable, or the atomic credential write cannot complete.

    Preconditions:
        The caller intentionally requested persistent credential storage and has
        permission to write the destination directory.

    Postconditions:
        On success the destination contains exactly one JSON ``api_key`` value.
        On failure the temporary file is removed and no partial replacement is
        reported as success.

    Side Effects:
        Creates directories and a temporary file, atomically replaces the
        destination, and requests owner-only permissions where supported.

    Security/Privacy:
        The key is written only to the selected file and is never returned in
        diagnostic text. Filesystem permission guarantees remain platform-owned.
    """

    key = validate_api_key(api_key)
    descriptor: int | None = None
    temporary: Path | None = None
    storage_failed = False
    try:
        destination = (path or credential_path()).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".credentials-",
            suffix=".tmp",
            dir=destination.parent,
            text=True,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump({"api_key": key}, handle)
            handle.write("\n")
        with suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(destination)
        with suppress(OSError):
            destination.chmod(0o600)
    except BaseException as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        if isinstance(exc, AttributeError | TypeError | ValueError | OSError):
            storage_failed = True
        else:
            raise
    if storage_failed:
        raise ConfigurationError("KUMA credential storage is unavailable") from None
    return destination


def read_stored_api_key(*, path: Path | None = None) -> str | None:
    """Read a stored credential only when its complete file is valid.

    Args:
        path: Credential file to read, or ``None`` for the platform default.

    Returns:
        Validated opaque key, or ``None`` when the file does not exist.

    Raises:
        ConfigurationError: If the file cannot be decoded as the expected JSON
            object or contains an invalid key.

    Side Effects:
        Reads one local file; performs no network request and writes nothing.

    Security/Privacy:
        Parse and validation failures use fixed messages and never echo file
        content or the candidate credential.
    """
    read_failed = False
    try:
        source = (path or credential_path()).expanduser()
        if not source.is_file():
            return None
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (AttributeError, TypeError, ValueError, OSError, json.JSONDecodeError):
        read_failed = True
    if read_failed:
        raise ConfigurationError(
            "The KUMA credential file is unreadable or invalid"
        ) from None
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
    """Resolve one API key using the documented precedence order.

    Args:
        explicit: Key passed by the caller. A truthy value has highest priority.
        environ: Environment mapping, or ``None`` for ``os.environ``. The
            resolver reads only ``KUMA_API_KEY`` from this mapping.
        stored_path: Optional credential file override used only when neither an
            explicit nor environment key is available.
        required: When ``True``, absence is an authentication error; when
            ``False``, absence returns ``None`` for credential-optional clients.

    Returns:
        Validated opaque key, or ``None`` only when ``required`` is ``False`` and
        all sources are absent.

    Raises:
        AuthenticationError: If a key is required but no source provides one.
        ConfigurationError: If the selected key or stored credential file is
            malformed.

    Side Effects:
        May read the environment and one credential file. It performs no network
        request and does not modify any source.

    Security/Privacy:
        Resolution never logs, embeds, or returns the key in an error message.
    """

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
    "DEFAULT_CASE_MAX_STEPS",
    "CreateRunConfig",
    "credential_path",
    "read_stored_api_key",
    "resolve_api_key",
    "resolve_create_run_config",
    "validate_api_key",
    "write_api_key",
]
