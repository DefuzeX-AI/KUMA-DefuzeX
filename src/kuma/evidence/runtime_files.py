"""Content-free, negotiated completeness of per-step file observations."""

from collections.abc import Mapping
from typing import Any

FILE_OBSERVATION_CAPABILITY = "file_observation_summary"
FILE_OBSERVATION_REASONS = (
    "not_enabled",
    "capture_failed",
    "capture_incomplete",
    "privacy_filtered",
    "path_filtered",
    "component_limit",
    "byte_limit",
    "legacy_metadata_unavailable",
)
_LOSS_REASONS = frozenset(FILE_OBSERVATION_REASONS[3:7])
_FIELDS = {"status", "observed_count", "retained_count", "omitted_count", "reasons"}


def validate_file_observation_summary(value: Any, *, retained: int) -> None:
    """Validate the closed summary against the actual wire file fact count.

    Runtime projection and its wire validator share this boundary. Counts are
    facts, not a census of unknown filesystem entries; a rename counts twice.
    Raise a static ValueError for malformed fields, status, order or arithmetic.
    No caller data is rendered in errors and no input is mutated or persisted.
    """
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise ValueError("runtime file observation summary is invalid")
    observed, kept, omitted = (
        value["observed_count"],
        value["retained_count"],
        value["omitted_count"],
    )
    counts = (observed, kept, omitted)
    reasons = value["reasons"]
    if (
        any(
            v is not None and (type(v) is not int or not 0 <= v <= 999_999_999)
            for v in counts
        )
        or kept is None
        or kept != retained
        or not isinstance(reasons, (list, tuple))
        or any(type(r) is not str or r not in FILE_OBSERVATION_REASONS for r in reasons)
        or list(reasons) != [r for r in FILE_OBSERVATION_REASONS if r in reasons]
    ):
        raise ValueError("runtime file observation summary is invalid")
    status = value["status"]
    if status in ("complete", "partial"):
        valid = (
            observed is not None and omitted is not None and observed == kept + omitted
        )
        if status == "complete":
            valid = valid and omitted == 0 and not reasons
        else:
            valid = (
                valid
                and bool(reasons)
                and set(reasons) <= (_LOSS_REASONS | {"capture_incomplete"})
                and (omitted > 0 or "capture_incomplete" in reasons)
                and (omitted == 0 or bool(set(reasons) & _LOSS_REASONS))
            )
    else:
        reason = (
            {
                "not_captured": "not_enabled",
                "unavailable": "capture_failed",
                "unknown": "legacy_metadata_unavailable",
            }.get(status)
            if isinstance(status, str)
            else None
        )
        valid = (
            reason is not None
            and observed is None
            and omitted is None
            and list(reasons) == [reason]
            and (status == "unknown" or kept == 0)
        )
    if not valid:
        raise ValueError("runtime file observation summary is invalid")


def file_observation_summary(
    *,
    retained: int,
    observed: int | None = None,
    reasons: tuple[str, ...] = (),
    status: str = "unknown",
) -> dict[str, Any]:
    """Create and validate detached local counters before Runtime serialization.

    The capture coordinator supplies authoritative enumerated fact counts.
    Missing historical metadata stays unknown; mixed Submission drop totals
    must never be supplied as observed. Return only closed low-sensitivity
    fields, or raise ValueError on impossible counts/status. No I/O occurs.
    """
    value = {
        "status": status,
        "observed_count": observed,
        "retained_count": retained,
        "omitted_count": None if observed is None else observed - retained,
        "reasons": [r for r in FILE_OBSERVATION_REASONS if r in reasons],
    }
    validate_file_observation_summary(value, retained=retained)
    return value


def omit_file_observations(summary: dict[str, Any], count: int, reason: str) -> None:
    """Account for known projection losses without inventing historical totals.

    The Runtime builder invokes this for privacy/path/count/byte filtering.
    Mutate only its detached local summary; unknown records stay unknown while
    retaining their exact remaining fact count. Invalid transitions raise a
    static ValueError. This does not count omitted diff bodies or log/Trace data.
    """
    if count == 0:
        return
    if type(count) is not int or count < 0 or reason not in _LOSS_REASONS:
        raise ValueError("runtime file observation summary is invalid")
    summary["retained_count"] -= count
    if summary["observed_count"] is not None:
        summary["omitted_count"] += count
        summary["status"] = "partial"
        summary["reasons"] = [
            r for r in FILE_OBSERVATION_REASONS if r in {*summary["reasons"], reason}
        ]
    validate_file_observation_summary(summary, retained=summary["retained_count"])


def summary_for_projection(value: Any, *, retained: int) -> dict[str, Any]:
    """Detach trusted-shape local metadata or explicitly mark legacy absence.

    Called before multipart construction; existing but malformed metadata is
    rejected, never repaired from mixed dropped_count. The returned list/dict
    owns its storage and may be fitted independently of immutable Run history.
    """
    if value is None:
        return file_observation_summary(
            retained=retained, reasons=("legacy_metadata_unavailable",)
        )
    validate_file_observation_summary(value, retained=retained)
    return {**value, "reasons": list(value["reasons"])}
