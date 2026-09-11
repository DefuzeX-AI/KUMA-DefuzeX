"""Closed, rubric-free Case artifacts shared by Run storage and official Judge."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .._json_values import detach_json
from ..contracts import Case
from ..errors import SensitiveDataError, ValidationError
from ..providers._official_wire import (
    canonical_sha256,
    validate_executed_strategy_group,
)
from ..providers.normalization import normalize_case
from .privacy import (
    contains_private_data,
    enforce_sensitive_policy,
    scan_sensitive_json,
)
from .tool_capabilities import _plain_json

CASE_ARTIFACT_VERSION = "kuma.case_artifact.v1"
MAX_CASE_ARTIFACT_BYTES = 5_242_880
_RAW_FIELDS = frozenset(
    {
        "schema_version",
        "batch_id",
        "case_id",
        "strategy_id",
        "strategy_version",
        "repo_fingerprint",
        "title",
        "description",
        "steps",
        "signature",
    }
)
_INTEGRITY_FIELDS = frozenset(
    {
        "batch_id",
        "case_sha256",
        "case_signature",
        "repo_fingerprint",
        "schema_version",
        "strategy_id",
        "strategy_version",
    }
)
_CASE_FIELDS = frozenset(
    {"case_id", "input_type", "input_schema", "inputs", "extensions"}
)
_INPUT_FIELDS = frozenset(
    {"input_id", "payload_type", "payload", "public_constraints", "extensions"}
)
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _invalid() -> ValidationError:
    """Create the content-free artifact error reused across decoding boundaries."""
    return ValidationError(
        "Case artifact is invalid or does not match its original content",
        code="case_artifact_invalid",
    )


def _object(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    """Require one already-detached object with exactly its frozen field set."""
    if type(value) is not dict or set(value) != fields:
        raise _invalid()
    return value


def _text(value: Any, maximum: int) -> bool:
    """Check the existing wire string bound without stripping public content."""
    return type(value) is str and 1 <= len(value) <= maximum


def artifact_json(value: Any) -> Any:
    """Detach finite JSON at root depth zero/max32 and map failures safely.

    Artifact load/save and official upload invoke this existing bounded JSON
    walker before schema/private checks. It neither truncates nor invokes tools;
    rejected graphs become case_artifact_invalid without their exception chain.
    """
    failure = False
    try:
        plain = _plain_json(detach_json(value))
        encoded = json.dumps(
            plain, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        failure = len(encoded) > MAX_CASE_ARTIFACT_BYTES
    except Exception:
        failure = True
    if failure:
        raise _invalid()
    return plain


def _privacy(value: Any) -> None:
    """Reject private grading fields and credentials without policy overrides."""
    if contains_private_data(
        value,
        extra_fields=(
            "rubric",
            "rubric_context",
            "run_id",
            "history",
            "evidence",
            "agent_output",
            "submission",
        ),
    ):
        raise SensitiveDataError(
            "Private grading fields cannot be stored in a Case artifact"
        )
    enforce_sensitive_policy(
        scan_sensitive_json(value, location="case_artifact"), allow_sensitive=False
    )


def validate_public_original(value: Any) -> dict[str, Any]:
    """Validate the exact original public Case and its non-secret checksum.

    Args:
        value: Ten-field server public Case, including its original signature.
    Returns:
        Detached unchanged content after length/step/checksum validation.
    Raises:
        ValidationError: case_artifact_invalid for malformed or changed content.
        SensitiveDataError: Private or sensitive content, regardless of overrides.
    Postconditions:
        SHA validation detects corruption only; it is not authenticity proof.
        Backend must read the tenant-owned original and bind its original Rubric.
    Side Effects:
        None; no server read, signature key, or filesystem access.
    """
    raw = _object(artifact_json(value), _RAW_FIELDS)
    _privacy(raw)
    limits = {
        "batch_id": 36,
        "case_id": 64,
        "strategy_id": 40,
        "strategy_version": 40,
        "title": 200,
        "description": 2000,
    }
    if raw["schema_version"] != "2" or any(
        not _text(raw[key], limit) for key, limit in limits.items()
    ):
        raise _invalid()
    valid_uuid = False
    try:
        valid_uuid = str(uuid.UUID(raw["batch_id"])) == raw["batch_id"]
    except ValueError:
        valid_uuid = False
    if (
        not valid_uuid
        or type(raw["repo_fingerprint"]) is not str
        or not _HEX.fullmatch(raw["repo_fingerprint"])
    ):
        raise _invalid()
    steps = raw["steps"]
    if type(steps) is not list or not 1 <= len(steps) <= 10:
        raise _invalid()
    ids: set[str] = set()
    for step in steps:
        _object(step, frozenset({"step_id", "prompt"}))
        if (
            not _text(step["step_id"], 80)
            or not _text(step["prompt"], 12000)
            or step["step_id"] in ids
        ):
            raise _invalid()
        ids.add(step["step_id"])
    unsigned = {key: value for key, value in raw.items() if key != "signature"}
    if raw["signature"] != "sha256:" + canonical_sha256(unsigned):
        raise _invalid()
    return raw


def _integrity(raw: dict[str, Any], value: Any) -> dict[str, Any]:
    """Bind closed artifact references to the single public original content."""
    if type(value) is not dict:
        raise _invalid()
    allowed = _INTEGRITY_FIELDS | (
        {"executed_strategy_group"} if "executed_strategy_group" in value else set()
    )
    refs = _object(value, frozenset(allowed))
    if any(
        refs[key] != raw[key]
        for key in (
            "batch_id",
            "repo_fingerprint",
            "schema_version",
            "strategy_id",
            "strategy_version",
        )
    ):
        raise _invalid()
    if refs["case_signature"] != raw["signature"] or refs[
        "case_sha256"
    ] != canonical_sha256(raw):
        raise _invalid()
    if "executed_strategy_group" in refs:
        invalid = False
        try:
            validate_executed_strategy_group(refs["executed_strategy_group"])
        except Exception:
            invalid = True
        if invalid:
            raise _invalid()
    return refs


def original_case_mapping(raw: dict[str, Any], refs: dict[str, Any]) -> dict[str, Any]:
    """Derive executable Inputs from the only saved original, retaining identity."""
    return {
        "case_id": raw["case_id"],
        "input_type": "text",
        "input_schema": None,
        "inputs": [
            {
                "input_id": step["step_id"],
                "payload_type": "text",
                "payload": step["prompt"],
                "public_constraints": {},
                "extensions": {},
            }
            for step in raw["steps"]
        ],
        "extensions": {"official_case": {**refs, "public_case": raw}},
    }


def case_content(case: Case) -> dict[str, Any]:
    """Project only reusable public Case fields, excluding Run and rubric slots."""
    return artifact_json(
        {
            "case_id": case.case_id,
            "input_type": case.input_type,
            "input_schema": case.input_schema,
            "extensions": case.extensions,
            "inputs": [
                {
                    "input_id": item.input_id,
                    "payload_type": item.payload_type,
                    "payload": item.payload,
                    "public_constraints": item.public_constraints,
                    "extensions": item.extensions,
                }
                for item in case.inputs
            ],
        }
    )


def official_original(case: Case) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind every current executable Input to the saved official public original.

    Run.save_case and OfficialJudge share this check before file/network effects.
    Missing historical originals, changed IDs/payload/type/constraints/extensions
    raise case_artifact_invalid, never a custom fallback. Checksums do not verify
    issuance or tenant ownership; the Backend still performs that authoritative read.
    """
    extension = artifact_json(case.extensions.get("official_case"))
    if type(extension) is not dict or "public_case" not in extension:
        raise _invalid()
    raw = validate_public_original(extension["public_case"])
    refs = _integrity(
        raw, {key: value for key, value in extension.items() if key != "public_case"}
    )
    expected = original_case_mapping(raw, refs)
    actual = case_content(case)
    if actual != expected:
        raise _invalid()
    return raw, refs


def _validate_custom(value: Any) -> dict[str, Any]:
    """Validate exact custom fields with the existing public Case normalizer."""
    content = _object(value, _CASE_FIELDS)
    if (
        type(content["extensions"]) is not dict
        or "official_case" in content["extensions"]
    ):
        raise ValidationError(
            "Custom Case cannot contain official origin", code="case_origin_invalid"
        )
    if (
        not _text(content["case_id"], 64)
        or type(content["inputs"]) is not list
        or not content["inputs"]
    ):
        raise _invalid()
    for item in content["inputs"]:
        _object(item, _INPUT_FIELDS)
        if not _text(item["input_id"], 80):
            raise _invalid()
    invalid = False
    try:
        normalized = normalize_case(
            content,
            run_id="artifact-validation",
            max_steps=len(content["inputs"]),
            required_input_type=None,
            required_input_schema=None,
        )
        invalid = case_content(normalized) != content
    except Exception:
        invalid = True
    if invalid:
        raise _invalid()
    return content


def validate_artifact(value: Any) -> dict[str, Any]:
    """Validate the frozen disk union before credentials, runtime, or transport.

    Args:
        value: Parsed UTF-8 artifact with exactly version, origin, case, integrity.
    Returns:
        Detached validated artifact; there is only one public content source.
    Raises:
        ValidationError: case_origin_invalid for absent/conflicting origin;
            case_artifact_invalid for shape/content/graph/size errors.
        SensitiveDataError: Credentials/private grading content; no override.
    Side Effects:
        None. Normalization validates custom inputs without invoking providers.
    """
    data = artifact_json(value)
    if type(data) is not dict or data.get("origin") not in ("official", "custom"):
        raise ValidationError(
            "Case artifact must declare its origin", code="case_origin_invalid"
        )
    _object(data, frozenset({"schema_version", "origin", "case", "integrity"}))
    if data["schema_version"] != CASE_ARTIFACT_VERSION:
        raise _invalid()
    _privacy(data)
    if data["origin"] == "official":
        raw = validate_public_original(data["case"])
        _integrity(raw, data["integrity"])
    else:
        if data["integrity"] is not None:
            raise ValidationError(
                "Custom Case cannot contain official integrity",
                code="case_origin_invalid",
            )
        _validate_custom(data["case"])
    return data


def artifact_from_case(case: Case) -> dict[str, Any]:
    """Serialize a Run-owned Case without exporting runtime or private state."""
    if "official_case" in case.extensions:
        content, integrity = official_original(case)
        origin = "official"
    else:
        content, integrity, origin = case_content(case), None, "custom"
    return validate_artifact(
        {
            "schema_version": CASE_ARTIFACT_VERSION,
            "origin": origin,
            "case": content,
            "integrity": integrity,
        }
    )


def artifact_case_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Return the validated artifact's sole content in the existing normalizer shape."""
    if data["origin"] == "official":
        return original_case_mapping(data["case"], data["integrity"])
    return data["case"]


class LoadedCaseProvider:
    """Internal prevalidated file source, distinct from a user custom Provider.

    Attributes:
        artifact: Closed local document validated before credential discovery.
        origin: Explicit official/custom discriminator, never inferred from IDs.
        count: Complete number of saved steps; used only as a nontruncating limit.
    """

    def __init__(self, artifact: dict[str, Any]) -> None:
        """Bind validated artifact content without reading or generating a Case."""
        self.artifact = artifact
        self.origin = artifact["origin"]
        self.count = len(
            artifact["case"]["steps" if self.origin == "official" else "inputs"]
        )

    def generate_case(self, context: Any) -> dict[str, Any]:
        """Supply the saved complete content to normal Run ID rebinding, with no I/O."""
        return artifact_case_mapping(self.artifact)
