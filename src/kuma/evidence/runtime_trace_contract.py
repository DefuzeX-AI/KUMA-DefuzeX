"""Closed validation of untrusted, normalized telemetry for Runtime Evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..repository.privacy import scan_sensitive_json
from ..repository.tool_capabilities import _plain_json
from .trace_tool_content import TOOL_CONTENT_KEYS, TOOL_METADATA_KEYS

TRACE_ARTIFACT_ID = "opentelemetry-trace-evidence"
TRACE_MEDIA_TYPE = "application/vnd.defuzex.trace-evidence+json"
TRACE_EXTRA_FIELDS = frozenset({"trace_evidence", "capture_status", "capture_summary"})
TRACE_MAX_SPANS = 10_000
TRACE_MAX_LINKS = 128
_BODY_STATUS = {"present", "not_recorded", "sensitive_content", "size_limit", "invalid"}
_SPAN_FIELDS = {
    "trace_id",
    "span_id",
    "parent_span_id",
    "name",
    "kind",
    "status",
    "start_time_unix_nano",
    "end_time_unix_nano",
    "duration_nano",
    "attributes",
    "events",
    "resource",
    "scope",
    "links",
}
_COUNTERS = {
    "observed_spans",
    "retained_spans",
    "dropped_spans",
    "dropped_attributes_events",
    "observed_log_records",
    "retained_log_records",
    "dropped_log_records",
    "dropped_log_fields",
}


def _require(condition: bool) -> None:
    """Raise a value-free contract error; callers translate it before transport."""
    if not condition:
        raise ValueError("runtime trace is invalid")


def _count(value: Any) -> bool:
    """Accept non-negative integer counters, never booleans."""
    return type(value) is int and value >= 0


def _identifier(value: Any, width: int) -> bool:
    """Accept fixed-width nonzero lowercase OTel identity without coercion."""
    return (
        isinstance(value, str)
        and re.fullmatch(f"[0-9a-f]{{{width}}}", value) is not None
        and int(value, 16) != 0
    )


def _attributes(value: Any, *, resource: bool = False, tool: bool = False) -> None:
    """Check the existing allowlist and safe values; tool JSON has its own bound."""
    from .runtime_contract import RUNTIME_AGENT_OUTPUT_MAX_BYTES
    from .trace_mapping import attribute_key_allowed

    _require(isinstance(value, Mapping))
    for key, child in value.items():
        _require(isinstance(key, str) and attribute_key_allowed(key, resource=resource))
        if key in TOOL_CONTENT_KEYS:
            _require(tool and not resource)
            plain = _plain_json(child)
            encoded = json.dumps(
                plain,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            _require(len(encoded) <= RUNTIME_AGENT_OUTPUT_MAX_BYTES)
        else:
            _require(not isinstance(child, Mapping))
            if key in TOOL_METADATA_KEYS:
                _require(tool and isinstance(child, str))


def _span(value: Any) -> None:
    """Validate normalized span identity, timing, real links and tool field states."""
    _require(isinstance(value, Mapping))
    tool = (
        isinstance(value.get("attributes"), Mapping)
        and value["attributes"].get("gen_ai.operation.name") == "execute_tool"
    )
    _require(set(value) == _SPAN_FIELDS | ({"tool_content_status"} if tool else set()))
    _require(_identifier(value["trace_id"], 32) and _identifier(value["span_id"], 16))
    parent = value["parent_span_id"]
    _require(parent is None or (_identifier(parent, 16) and parent != value["span_id"]))
    _require(isinstance(value["name"], str))
    _require(
        value["kind"]
        in {"internal", "server", "client", "producer", "consumer", "unspecified"}
    )
    _require(value["status"] in {"unset", "ok", "error"})
    _require(
        all(
            _count(value[field])
            for field in ("start_time_unix_nano", "end_time_unix_nano", "duration_nano")
        )
    )
    _require(
        value["end_time_unix_nano"] - value["start_time_unix_nano"]
        == value["duration_nano"]
    )
    _attributes(value["attributes"], tool=tool)
    _attributes(value["resource"], resource=True)
    _require(
        isinstance(value["scope"], Mapping)
        and set(value["scope"]) == {"name", "version"}
        and all(isinstance(v, str) for v in value["scope"].values())
    )
    _require(isinstance(value["events"], (list, tuple)))
    for event in value["events"]:
        _require(
            isinstance(event, Mapping)
            and set(event) == {"name", "time_unix_nano", "attributes"}
        )
        _require(isinstance(event["name"], str) and _count(event["time_unix_nano"]))
        _attributes(event["attributes"])
    _require(
        isinstance(value["links"], (list, tuple))
        and len(value["links"]) <= TRACE_MAX_LINKS
    )
    links: set[tuple[str, str]] = set()
    for link in value["links"]:
        _require(isinstance(link, Mapping) and set(link) == {"trace_id", "span_id"})
        _require(_identifier(link["trace_id"], 32) and _identifier(link["span_id"], 16))
        pair = (link["trace_id"], link["span_id"])
        _require(pair not in links)
        links.add(pair)
    if tool:
        states = value["tool_content_status"]
        _require(isinstance(states, Mapping) and set(states) == {"arguments", "result"})
        for key, field in TOOL_CONTENT_KEYS.items():
            _require(states[field] in _BODY_STATUS)
            _require((states[field] == "present") == (key in value["attributes"]))


def _validate_trace_artifact(
    component: Mapping[str, Any],
    *,
    run_id: str,
    input_id: str,
    case_id: str | None = None,
) -> None:
    """Validate one hash-bound telemetry artifact, never a trusted execution fact.

    Args:
        component: Runtime artifact with the three required trace extension fields.
        run_id: Expected outer Run association.
        input_id: Expected outer Input association.
        case_id: Actual Judge Case identity when available at the upload boundary.

    Raises:
        ValueError: On malformed identities, fields, counters, budgets or hashes.

    Postconditions:
        Present telemetry is closed and associated, with retained-span accounting
        and no invented success from UNSET. Hashes provide consistency, not trust.

    Security/Privacy:
        Canonical sensitive scanning applies to all retained content. No I/O,
        provider invocation, payload repr or authorization assertion is made.
    """
    from .runtime_contract import RUNTIME_EVIDENCE_MAX_BYTES, runtime_evidence_json
    from .trace import _CAPTURE_REASONS

    _require(
        component.get("artifact_id") == TRACE_ARTIFACT_ID
        and component.get("media_type") == TRACE_MEDIA_TYPE
    )
    trace = component["trace_evidence"]
    _require(
        isinstance(trace, Mapping)
        and set(trace)
        == {
            "schema_version",
            "run_id",
            "case_id",
            "input_id",
            "spans",
            "dropped_count",
            "truncated",
            "reasons",
        }
    )
    _require(
        trace["schema_version"] == "defuzex.trace_evidence.v1"
        and trace["run_id"] == run_id
        and trace["input_id"] == input_id
    )
    _require(
        isinstance(trace["case_id"], str)
        and bool(trace["case_id"])
        and (case_id is None or trace["case_id"] == case_id)
    )
    _require(_count(trace["dropped_count"]) and type(trace["truncated"]) is bool)
    reasons = trace["reasons"]
    _require(
        isinstance(reasons, (list, tuple))
        and all(isinstance(r, str) and r in _CAPTURE_REASONS for r in reasons)
        and len(set(reasons)) == len(reasons)
    )
    spans = trace["spans"]
    _require(isinstance(spans, (list, tuple)) and len(spans) <= TRACE_MAX_SPANS)
    identities = set()
    for span in spans:
        _span(span)
        identity = (span["trace_id"], span["span_id"])
        _require(identity not in identities)
        identities.add(identity)
    _validate_capture(component, trace, identities)
    encoded = runtime_evidence_json(trace).encode("utf-8")
    _require(
        len(encoded) <= RUNTIME_EVIDENCE_MAX_BYTES
        and component["size_bytes"] == len(encoded)
        and component["sha256"] == hashlib.sha256(encoded).hexdigest()
    )
    _require(not scan_sensitive_json(trace, location="runtime_trace"))


def _validate_capture(
    component: Mapping[str, Any],
    trace: Mapping[str, Any],
    identities: set[tuple[str, str]],
) -> None:
    """Check observable loss/status counts and acyclic parent relationships.

    The artifact validator supplies shaped spans; no execution or authorization
    claim is inferred. Flush failures may add drops without an observed span.
    """
    spans = trace["spans"]
    reasons = trace["reasons"]
    summary = component["capture_summary"]
    _require(
        isinstance(summary, Mapping)
        and set(summary)
        == _COUNTERS | {"schema_version", "sampling_policy", "topology_complete"}
    )
    _require(
        summary["schema_version"] == "defuzex.trace_capture_summary.v1"
        and summary["sampling_policy"] == "deterministic_topology_v1"
    )
    _require(
        all(_count(summary[k]) for k in _COUNTERS)
        and type(summary["topology_complete"]) is bool
    )
    _require(summary["retained_spans"] == len(spans))
    for observed, retained, dropped in (
        ("observed_spans", "retained_spans", "dropped_spans"),
        ("observed_log_records", "retained_log_records", "dropped_log_records"),
    ):
        _require(
            summary[observed] >= summary[retained]
            and summary[dropped] == summary[observed] - summary[retained]
        )
    status = component["capture_status"]
    _require(status in {"complete", "partial", "failed"})
    degraded = bool(reasons or trace["truncated"] or trace["dropped_count"])
    _require(status == (("partial" if spans else "failed") if degraded else "complete"))
    _require(trace["dropped_count"] <= 999_999_999)
    _require(
        trace["dropped_count"]
        >= min(
            summary["dropped_spans"] + summary["dropped_attributes_events"], 999_999_999
        )
    )
    _require(summary["topology_complete"] == ("trace_topology_partial" not in reasons))
    _require(
        status != "complete"
        or (
            not reasons
            and not trace["truncated"]
            and trace["dropped_count"] == 0
            and summary["dropped_spans"] == 0
            and summary["dropped_attributes_events"] == 0
        )
    )
    _require(status != "failed" or not spans)
    omitted_fields = 0
    parents = {
        (span["trace_id"], span["span_id"]): (span["trace_id"], span["parent_span_id"])
        for span in spans
    }
    checked = set()
    for identity in parents:
        path = set()
        current = identity
        while current in parents and current not in checked:
            _require(current not in path)
            path.add(current)
            current = parents[current]
        checked.update(path)
    for span in spans:
        if (
            span["parent_span_id"] is not None
            and (span["trace_id"], span["parent_span_id"]) not in identities
        ):
            _require(
                status == "partial"
                and not summary["topology_complete"]
                and "trace_topology_partial" in reasons
            )
        if "tool_content_status" in span and any(
            v != "present" for v in span["tool_content_status"].values()
        ):
            _require(status != "complete")
            for state in span["tool_content_status"].values():
                if state == "present":
                    continue
                suffix = "sensitive" if state == "sensitive_content" else state
                _require(f"trace_tool_content_{suffix}" in reasons)
                omitted_fields += int(state != "not_recorded")
    _require(omitted_fields <= trace["dropped_count"])


def validate_trace_artifact(
    component: Mapping[str, Any],
    *,
    run_id: str,
    input_id: str,
    case_id: str | None = None,
) -> None:
    """Enforce the Trace contract without exposing malformed values or causes.

    Runtime projection and final upload supply the owning Run/Input/Case IDs.
    Success means the closed telemetry schema, hashes and observed completeness
    agree, not that execution or authority is proven. Any malformed field,
    graph, privacy violation or accessor failure becomes a value-free
    ValueError with no raw exception chain. No I/O or mutation is performed.
    """
    invalid = False
    try:
        _validate_trace_artifact(
            component, run_id=run_id, input_id=input_id, case_id=case_id
        )
    except Exception:
        invalid = True
    if invalid:
        raise ValueError("runtime trace is invalid")


def validate_trace_components(
    components: Sequence[Mapping[str, Any]],
    *,
    enabled: bool,
    run_id: str,
    input_id: str,
) -> None:
    """Require exactly one trace body only when its named capability is declared."""
    found = 0
    for component in components:
        if set(component) & TRACE_EXTRA_FIELDS:
            _require(enabled and set(component) >= TRACE_EXTRA_FIELDS)
            validate_trace_artifact(component, run_id=run_id, input_id=input_id)
            found += 1
    _require(found == int(enabled))
