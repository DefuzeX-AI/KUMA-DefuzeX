"""Negotiate detailed Judge results and append bounded typed message Evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ..errors import LimitExceededError, ProviderError
from ..evidence.assessment_contract import (
    ASSESSMENT_CONTRACT,
    ASSESSMENT_EVIDENCE_MAX_BYTES,
    ASSESSMENT_EVIDENCE_MEDIA_TYPE,
    ASSESSMENT_EVIDENCE_SCHEMA,
    _validated_provenance,
    assessment_json,
    build_assessment_evidence,
)
from ..evidence.runtime import runtime_submission_id
from ..evidence.runtime_actors import (
    invalid_runtime_actor,
    selected_span_index,
    validate_runtime_actors,
)
from ..evidence.runtime_contract import RUNTIME_EVIDENCE_MEDIA_TYPE
from ..transport.backend import UploadPart
from ._official_wire import plain_json


def negotiate_assessment(
    requested: str | None, config: Any, context: Any
) -> str | None:
    """Select only advertised contracts; explicit messages/requests never downgrade.

    Auto selects detailed assessment on a supporting service and returns None on
    a legacy service. None explicitly disables detailed assessment. A message
    bundle requires support rather than being silently discarded. No I/O occurs.
    """
    messages = any(
        "assessment_evidence" in item.submission.extensions for item in context.history
    )
    supported = ASSESSMENT_CONTRACT in config.supported_assessment_contracts
    if requested is None:
        if messages:
            raise ProviderError(
                "Public messages require detailed assessment",
                code="assessment_unsupported",
            )
        return None
    if not supported:
        if requested == ASSESSMENT_CONTRACT or messages:
            raise ProviderError(
                "The service does not support detailed assessment",
                code="assessment_unsupported",
            )
        return None
    if messages and ASSESSMENT_EVIDENCE_SCHEMA not in config.evidence_types:
        raise ProviderError(
            "The service does not support public message Evidence",
            code="assessment_unsupported",
        )
    return ASSESSMENT_CONTRACT


def _envelope(item: Any) -> dict[str, Any]:
    """Revalidate immutable Submission association and exact closed message data."""
    value = plain_json(item.submission.extensions["assessment_evidence"])
    names = {
        "schema_version",
        "run_id",
        "input_id",
        "step_id",
        "submission_id",
        "public_messages",
        "provenance",
        "coverage",
    }
    if not isinstance(value, Mapping) or set(value) != names:
        raise ProviderError(
            "Invalid assessment Evidence", code="assessment_evidence_invalid"
        )
    expected = build_assessment_evidence(
        {
            "public_messages": value["public_messages"],
            "coverage": value["coverage"],
            "provenance": value["provenance"],
        },
        run_id=item.submission.run_id,
        input_id=item.submission.input_id,
        step_id=item.test_input.input_id,
        submission_id=runtime_submission_id(
            item.submission.run_id, item.submission.input_id
        ),
    )
    if expected != value:
        raise ProviderError(
            "Invalid assessment Evidence association",
            code="assessment_evidence_invalid",
        )
    return expected


def _validate_provenance(
    envelope: Mapping[str, Any], parts: tuple[UploadPart, ...]
) -> None:
    """Bind declared actors to existing JSON parts, not arbitrary host data.

    The reference ordinal excludes the Case part and precedes appended assessment
    envelopes. Hashes bind exact retained UTF-8 bytes. Pointers resolve only within
    those JSON parts; missing/non-JSON/self references fail before multipart POST.
    No claim is made that a caller's actor declaration is independently verified.
    """
    for entry in envelope["provenance"]:
        ref = entry["reference"]
        index = ref["evidence_index"]
        invalid = False
        try:
            part = parts[index]
            if hashlib.sha256(part.data).hexdigest() != ref["evidence_sha256"]:
                raise ValueError("Reference mismatch")
            value = json.loads(part.data)
            _runtime_reference(envelope, ref, part, value)
            for segment in ref["pointer"].split("/")[1:]:
                key = segment.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                        raise ValueError("Invalid array pointer")
                    value = value[int(key)]
                else:
                    value = value[key]
        except (IndexError, KeyError, TypeError, ValueError, UnicodeError):
            invalid = True
        if invalid:
            raise ProviderError(
                "Invalid assessment provenance", code="assessment_evidence_invalid"
            )


def _runtime_reference(
    envelope: Mapping[str, Any],
    reference: Mapping[str, Any],
    part: UploadPart,
    value: Any,
) -> None:
    """Reject legacy/root/foreign-step references before assigning actor authority.

    Only already validated runtime parts may carry component/span declarations.
    Core owns semantic witness resolution. This check cannot turn raw_log JSON,
    an assessment message or a cross-case reproduction into a runtime component.
    """
    match = re.match(r"^/components/(0|[1-9][0-9]*)(?:/|$)", reference["pointer"])
    fields = ("run_id", "input_id", "step_id", "submission_id")
    if (
        part.content_type != RUNTIME_EVIDENCE_MEDIA_TYPE
        or not isinstance(value, Mapping)
        or match is None
        or any(value.get(key) != envelope[key] for key in fields)
    ):
        raise ValueError("Invalid runtime provenance")
    components = value.get("components")
    if (
        not isinstance(components, list)
        or int(match[1]) >= len(components)
        or not isinstance(components[int(match[1])], Mapping)
    ):
        raise ValueError("Invalid runtime component")


def _bind_runtime_actors(
    envelope: dict[str, Any], item: Any, parts: tuple[UploadPart, ...]
) -> None:
    """Resolve local span declarations against final serialized runtime bytes.

    Official upload calls this after schema negotiation/redaction and before
    multipart/POST. Only same-submission typed runtime spans are eligible. The
    exact ordinal, SHA256 and leaf span pointer are merged into frozen provenance;
    missing/ambiguous selectors and conflicting explicit references fail closed.
    No wire fields or coverage statuses are added; local selectors never leave
    the SDK. Caller-declared actors are not independent proof of attribution.
    """
    selectors = validate_runtime_actors(
        plain_json(item.submission.extensions.get("runtime_actors"))
    )
    if not selectors:
        return
    spans, references = [], []
    fields = ("run_id", "input_id", "step_id", "submission_id")
    for ordinal, part in enumerate(parts):
        if part.content_type != RUNTIME_EVIDENCE_MEDIA_TYPE:
            continue
        value = json.loads(part.data)
        if any(value.get(key) != envelope[key] for key in fields):
            continue
        digest = hashlib.sha256(part.data).hexdigest()
        for component_index, component in enumerate(value["components"]):
            for span_index, span in enumerate(
                component.get("trace_evidence", {}).get("spans", ())
            ):
                spans.append(span)
                references.append(
                    {
                        "evidence_index": ordinal,
                        "evidence_sha256": digest,
                        "pointer": f"/components/{component_index}/trace_evidence/spans/{span_index}",
                    }
                )
    declarations = list(envelope["provenance"])
    for selector in selectors:
        index = selected_span_index(selector, spans)
        declarations.append(
            {"reference": references[index], "actor": selector["actor"]}
        )
    invalid = False
    try:
        _validated_provenance(declarations)
    except ValueError:
        invalid = True
    if invalid:
        raise invalid_runtime_actor()
    envelope["provenance"] = declarations


def append_assessment_parts(
    context: Any,
    config: Any,
    parts: tuple[UploadPart, ...],
    manifest: dict[str, Any],
    *,
    part_prefix: str,
) -> tuple[tuple[UploadPart, ...], dict[str, Any]]:
    """Append one assessment part per supplied step without altering old parts.

    Existing Evidence ordering stays stable for hash-bound references. New parts
    use the existing multipart manifest, per-file and aggregate budgets, never
    raw_log fallback. Only the bounded redacted envelope is serialized; no
    private assessment/Rubric or original native transcript is persisted.
    Single requests repeat the Backend's ``logs`` field; nonempty batch prefixes
    retain unique item-scoped names for manifest binding.
    """
    result, entries = list(parts), list(manifest["files"])
    message_count = 0
    for index, item in enumerate(context.history):
        if "assessment_evidence" not in item.submission.extensions:
            continue
        envelope = _envelope(item)
        _bind_runtime_actors(envelope, item, parts)
        message_count += len(envelope["public_messages"])
        if message_count > 200:
            raise LimitExceededError(
                "Public message count exceeds 200 per request",
                code="public_messages_too_large",
            )
        _validate_provenance(envelope, parts)
        encoded = assessment_json(envelope)
        if len(encoded) > min(config.max_file_bytes, ASSESSMENT_EVIDENCE_MAX_BYTES):
            raise LimitExceededError(
                "Assessment Evidence exceeds the upload limit", code="log_size_exceeded"
            )
        name = f"kuma-assessment-{index}.json"
        result.append(
            UploadPart(
                name=f"{part_prefix}assessment_{index}" if part_prefix else "logs",
                filename=name,
                content_type=ASSESSMENT_EVIDENCE_MEDIA_TYPE,
                data=encoded,
            )
        )
        entries.append(
            {
                "name": name,
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "evidence_type": ASSESSMENT_EVIDENCE_SCHEMA,
            }
        )
    if (
        len(result) > config.max_files
        or sum(len(part.data) for part in result) > config.max_total_bytes
    ):
        raise LimitExceededError(
            "Assessment Evidence exceeds the upload limit", code="log_size_exceeded"
        )
    return tuple(result), {**manifest, "files": entries}
