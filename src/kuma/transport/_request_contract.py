"""Closed local and Backend recovery contracts for official requests."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..errors import ProviderError, ValidationError
from ..repository.strategy_groups import validate_strategy_group_wire_selection
from .backend import validate_client_request_id
from .operations import PendingOperation

REQUEST_RECORD_SCHEMA = "kuma.request_record.v1"
REQUEST_RECOVERY_SCHEMA = "kuma.request_recovery.v1"
REQUEST_TYPES = {"case_generation", "judgment"}
STATUSES = {"prepared", "queued", "running", "succeeded", "failed"}
REMOTE_STATUSES = {"queued", "running", "succeeded", "failed"}
MAX_RECORD_BYTES = 8_192
MAX_PUBLIC_REPORT_BYTES = 1_048_576
MAX_RECORDS = 4_096
_MAX_OPERATION_ID_CHARS = 64
_HEX_64 = set("0123456789abcdef")


def new_client_request_id() -> str:
    """Return ``kreq_`` plus 128 random bits without embedding request data."""
    return f"kreq_{secrets.token_hex(16)}"


def canonical_request_sha256(value: Mapping[str, Any]) -> str:
    """Hash a finite canonical request projection without persisting its body.

    Multipart callers provide ordinary fields and per-part hashes rather than
    raw Evidence. Invalid or non-finite JSON raises ``request_state_invalid``.
    """
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ProviderError(
            "The request recovery identity is invalid",
            code="request_state_invalid",
        ) from None
    return hashlib.sha256(encoded).hexdigest()


def backend_identity(base_url: str) -> str:
    """Return a non-secret SHA-256 binding for the normalized Backend URL."""
    return hashlib.sha256(base_url.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """Expose bounded non-secret metadata for one asynchronous request.

    Attributes:
        client_request_id: Stable local and Backend recovery handle.
        request_type: ``case_generation`` or ``judgment``.
        status: Prepared, active, or terminal lifecycle state.
        operation_id: Opaque public Backend operation ID when known.
        run_id: Public Run correlation for Judge requests when known.
        case_id: Public Case correlation when known.
        result_locator: Repository-relative public report path when saved.
        created_at: Local UNIX timestamp when the record was prepared.
        updated_at: Local UNIX timestamp of its last committed transition.

    API keys, bodies, Evidence, Rubrics, responses, and error text are absent.
    """

    client_request_id: str
    request_type: str
    status: str
    operation_id: str | None
    run_id: str | None
    case_id: str | None
    result_locator: str | None
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        """Return the exact JSON-safe public request summary."""
        return {
            "schema_version": REQUEST_RECORD_SCHEMA,
            "client_request_id": self.client_request_id,
            "request_type": self.request_type,
            "status": self.status,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "result_locator": self.result_locator,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class StoredRequest:
    """Represent the complete private-on-disk recovery record.

    Attributes:
        public: Non-secret projection exposed by list/show APIs.
        idempotency_key: Stable Backend replay key, never printed publicly.
        request_sha256: Digest binding the record to exact request material.
        backend_sha256: Digest of the normalized public Backend URL.
        api_key_sha256: Digest of the exact creating API key.
        case_validation: Low-sensitive recovered Case validation context.
        error_code: Stable terminal public error code without details.
        error_retryable: Retry flag paired with a terminal error code.
        assessment_contract: Optional negotiated Judge result schema, retained
            across process restart without storing Evidence or assessment bodies.
    """

    public: RequestRecord
    idempotency_key: str
    request_sha256: str
    backend_sha256: str
    api_key_sha256: str
    case_validation: Mapping[str, Any] | None
    error_code: str | None = None
    error_retryable: bool | None = None
    assessment_contract: str | None = None


def validate_recovery_response(
    value: Mapping[str, Any], *, client_request_id: str, request_type: str
) -> tuple[str, str]:
    """Validate the exact closed authenticated Backend recovery response."""
    expected = {
        "schema_version",
        "client_request_id",
        "request_type",
        "operation_id",
        "status",
    }
    if (
        set(value) != expected
        or value.get("schema_version") != REQUEST_RECOVERY_SCHEMA
        or value.get("client_request_id") != client_request_id
        or value.get("request_type") != request_type
        or value.get("status") not in REMOTE_STATUSES
    ):
        raise ProviderError(
            "The Backend returned an invalid request recovery response",
            code="invalid_response",
        )
    return validate_operation_id(value.get("operation_id")), str(value["status"])


def stored_payload(stored: StoredRequest) -> dict[str, Any]:
    """Return the exact closed on-disk JSON representation."""
    return {
        **stored.public.to_dict(),
        "idempotency_key": stored.idempotency_key,
        "request_sha256": stored.request_sha256,
        "backend_sha256": stored.backend_sha256,
        "api_key_sha256": stored.api_key_sha256,
        "case_validation": stored.case_validation,
        "error_code": stored.error_code,
        "error_retryable": stored.error_retryable,
        **(
            {}
            if stored.assessment_contract is None
            else {"assessment_contract": stored.assessment_contract}
        ),
    }


def read_record(path: Path) -> StoredRequest:
    """Read one bounded record through a descriptor bound to its safe pathname.

    The post-open identity checks occur before any bytes are read, preventing a
    symlink or parent-component swap from redirecting the ledger read outside
    the canonical repository root. Every failure closes the descriptor and
    exposes only stable local-state errors.
    """
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        frozen = os.fstat(descriptor)
        canonical = path.resolve(strict=True)
        named = path.lstat()
        root = path.parents[2]
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            canonical != path
            or not stat.S_ISREG(frozen.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (reparse and getattr(named, "st_file_attributes", 0) & reparse)
            or (frozen.st_dev, frozen.st_ino) != (named.st_dev, named.st_ino)
            or frozen.st_dev != root.stat().st_dev
            or frozen.st_size > MAX_RECORD_BYTES
        ):
            raise OSError
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise OSError
        return validate_stored(json.loads(raw.decode("utf-8")))
    except FileNotFoundError:
        raise ProviderError(
            "Request record was not found", code="request_not_found"
        ) from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProviderError(
            "The request record is unreadable", code="request_state_invalid"
        ) from None
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def validate_stored(raw: Any) -> StoredRequest:
    """Validate all persisted fields and reject unknown or private additions."""
    fields = {
        "schema_version",
        "client_request_id",
        "request_type",
        "status",
        "operation_id",
        "run_id",
        "case_id",
        "result_locator",
        "created_at",
        "updated_at",
        "idempotency_key",
        "request_sha256",
        "backend_sha256",
        "api_key_sha256",
        "case_validation",
        "error_code",
        "error_retryable",
    }
    if isinstance(raw, Mapping) and "assessment_contract" in raw:
        fields.add("assessment_contract")
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise ProviderError("Request record is invalid", code="request_state_invalid")
    try:
        client_id = validate_client_request_id(raw["client_request_id"])
    except Exception:
        raise ProviderError(
            "Request record is invalid", code="request_state_invalid"
        ) from None
    request_type = raw["request_type"]
    status = raw["status"]
    created = raw["created_at"]
    updated = raw["updated_at"]
    operation_id = raw["operation_id"]
    error_code = raw["error_code"]
    error_retryable = raw["error_retryable"]
    valid = (
        raw["schema_version"] == REQUEST_RECORD_SCHEMA
        and request_type in REQUEST_TYPES
        and status in STATUSES
        and valid_timestamp(created)
        and valid_timestamp(updated)
        and updated >= created
        and (operation_id is None or is_operation_id(operation_id))
        and is_idempotency_key(raw["idempotency_key"])
        and is_hash(raw["request_sha256"])
        and is_hash(raw["backend_sha256"])
        and is_hash(raw["api_key_sha256"])
        and (error_code is None or is_safe_code(error_code))
        and (error_retryable is None or isinstance(error_retryable, bool))
        and ((status == "failed") == (error_code is not None))
        and ((status == "failed") == (error_retryable is not None))
        and (status == "prepared" or operation_id is not None)
        and (
            "assessment_contract" not in raw
            or (
                raw["assessment_contract"] == "kuma.judge_assessment.v1"
                and request_type == "judgment"
            )
        )
    )
    if not valid:
        raise ProviderError("Request record is invalid", code="request_state_invalid")
    public = RequestRecord(
        client_request_id=client_id,
        request_type=str(request_type),
        status=str(status),
        operation_id=operation_id,
        run_id=optional_identifier(raw["run_id"], 128),
        case_id=optional_identifier(raw["case_id"], 128),
        result_locator=validate_locator(raw["result_locator"]),
        created_at=float(created),
        updated_at=float(updated),
    )
    return StoredRequest(
        public=public,
        idempotency_key=raw["idempotency_key"],
        request_sha256=raw["request_sha256"],
        backend_sha256=raw["backend_sha256"],
        api_key_sha256=raw["api_key_sha256"],
        case_validation=validate_case_context(raw["case_validation"]),
        error_code=error_code,
        error_retryable=error_retryable,
        assessment_contract=raw.get("assessment_contract"),
    )


def pending_operation(stored: StoredRequest) -> PendingOperation:
    """Project a durable request record to the operation coordinator type."""
    return PendingOperation(
        operation_type=stored.public.request_type,
        idempotency_key=stored.idempotency_key,
        base_url_identity=stored.backend_sha256,
        created_at=stored.public.created_at,
        updated_at=stored.public.updated_at,
        operation_id=stored.public.operation_id,
    )


def validate_case_context(
    value: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Validate low-sensitive fields needed to check a recovered Case result."""
    if value is None:
        return None
    keys = {"repo_fingerprint", "max_steps", "strategy_id", "strategy_group_selection"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProviderError(
            "Case recovery context is invalid", code="request_state_invalid"
        )
    maximum = value["max_steps"]
    group = value["strategy_group_selection"]
    valid = (
        isinstance(value["repo_fingerprint"], str)
        and 1 <= len(value["repo_fingerprint"]) <= 128
        and isinstance(value["strategy_id"], str)
        and 1 <= len(value["strategy_id"]) <= 80
        and not isinstance(maximum, bool)
        and isinstance(maximum, int)
        and 1 <= maximum <= 1_000_000
        and (group is None or isinstance(group, Mapping))
    )
    if not valid:
        raise ProviderError(
            "Case recovery context is invalid", code="request_state_invalid"
        )
    try:
        return {
            "repo_fingerprint": value["repo_fingerprint"],
            "max_steps": maximum,
            "strategy_id": value["strategy_id"],
            "strategy_group_selection": (
                None if group is None else validate_strategy_group_wire_selection(group)
            ),
        }
    except (TypeError, ValueError, ProviderError, ValidationError):
        raise ProviderError(
            "Case recovery context is invalid", code="request_state_invalid"
        ) from None


def validate_locator(value: str | None) -> str | None:
    """Accept only a bounded repository-relative public result path."""
    if value is None:
        return None
    try:
        path = Path(value)
        valid = (
            isinstance(value, str)
            and 1 <= len(value) <= 240
            and not path.is_absolute()
            and not PurePosixPath(value).is_absolute()
            and not PureWindowsPath(value).is_absolute()
            and not PureWindowsPath(value).drive
            and ".." not in path.parts
            and "\0" not in value
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ProviderError("Result locator is invalid", code="request_state_invalid")
    return value.replace("\\", "/")


def optional_identifier(value: Any, maximum: int) -> str | None:
    """Validate optional printable correlation text without echoing it on error."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ProviderError(
            "Request correlation is invalid", code="request_state_invalid"
        )
    return value


def validate_operation_id(value: Any) -> str:
    """Return one bounded opaque operation ID or fail closed."""
    if not is_operation_id(value):
        raise ProviderError("Operation ID is invalid", code="invalid_response")
    return value


def is_operation_id(value: Any) -> bool:
    """Return whether an operation ID is bounded printable ASCII."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAX_OPERATION_ID_CHARS
        and all(32 <= ord(char) < 127 for char in value)
    )


def require_hash(value: Any, field: str) -> str:
    """Return one lowercase SHA-256 or raise a stable local-state error."""
    if not is_hash(value):
        raise ProviderError(f"{field} is invalid", code="request_state_invalid")
    return value


def is_hash(value: Any) -> bool:
    """Return whether a value is an exact lowercase SHA-256 digest."""
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_64


def validate_idempotency_key(value: Any) -> str:
    """Validate a stable replay key without placing it in an error."""
    if not is_idempotency_key(value):
        raise ProviderError("Idempotency key is invalid", code="request_state_invalid")
    return value


def is_idempotency_key(value: Any) -> bool:
    """Return whether a key is bounded printable non-space ASCII."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 255
        and all(33 <= ord(char) <= 126 for char in value)
    )


def safe_code(value: Any) -> str:
    """Validate a stable public error code before local persistence."""
    if not is_safe_code(value):
        raise ProviderError("Error code is invalid", code="request_state_invalid")
    return value


def is_safe_code(value: Any) -> bool:
    """Return whether a value is a bounded lowercase public code."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and all(char.islower() or char.isdigit() or char == "_" for char in value)
    )


def valid_timestamp(value: Any) -> bool:
    """Return whether a value is a finite nonnegative UNIX timestamp."""
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value >= 0
    )
