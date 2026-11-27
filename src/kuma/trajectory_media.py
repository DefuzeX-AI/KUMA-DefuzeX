"""Closed media-omission coverage validation; never interprets or rewrites source media."""

import re


def _check(condition: bool) -> None:
    """Signal a safe malformed-wire failure to the trajectory response boundary."""
    if not condition:
        raise ValueError("Invalid media omission coverage")


def _number(value: object, maximum: int = 8388608) -> int:
    """Require a nonnegative JSON integer without treating booleans as coordinates."""
    _check(type(value) is int and 0 <= value <= maximum)
    return value


def _ranges(value: object, length: int, maximum: int) -> list[list[int]]:
    """Validate bounded ordered disjoint half-open ranges in original character coordinates."""
    _check(isinstance(value, list) and len(value) <= maximum)
    end = 0
    for index, pair in enumerate(value):
        _check(isinstance(pair, list) and len(pair) == 2)
        start, stop = (_number(v) for v in pair)
        _check((end < start if index else end <= start) and start < stop <= length)
        end = stop
    return value


def _union(ranges: list[list[int]]) -> list[list[int]]:
    """Merge overlap/adjacency for exact coverage arithmetic without changing source indices."""
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _complement(ranges: list[list[int]], length: int) -> list[list[int]]:
    """Return all uncovered character intervals within one original source."""
    result, end = [], 0
    for start, stop in _union(ranges):
        if end < start:
            result.append([end, start])
        end = stop
    if end < length:
        result.append([end, length])
    return result


def _omissions(media: dict, sources: dict) -> dict:
    """Validate closed omission metadata and canonical source rank, returning original ranges.

    This checks declared coordinates and hash shapes, not source-byte authenticity;
    Core must recompute those values using the immutable source unavailable here.
    """
    _check(
        isinstance(media, dict)
        and set(media)
        == {
            "schema_version",
            "scope",
            "reason",
            "omissions",
            "reviewable_characters",
            "reviewable_delivery_complete",
            "media_understanding",
        }
    )
    _check(
        media["schema_version"] == "kuma.media_omission.v1"
        and media["scope"] == "text_and_structure"
        and media["reason"] == "unsupported_media_content"
        and media["media_understanding"] == "not_performed"
    )
    _check(type(media["reviewable_delivery_complete"]) is bool)
    omitted = {key: [] for key in sources}
    items = media["omissions"]
    _check(isinstance(items, list) and 1 <= len(items) <= 1024)
    previous = (-1, -1)
    for item in items:
        _check(
            isinstance(item, dict)
            and set(item)
            == {
                "evidence_id",
                "offset",
                "end",
                "media_kind",
                "utf8_bytes",
                "sha256",
            }
        )
        identity = item["evidence_id"]
        _check(isinstance(identity, str) and identity in sources)
        if identity in {"task", "tool-catalog"}:
            rank = 0 if identity == "task" else 1
        else:
            _check(re.fullmatch(r"event-(0|[1-9][0-9]*)", identity) is not None)
            rank = int(identity[6:]) + 2
        start, end = _number(item["offset"]), _number(item["end"])
        _check(previous < (rank, start))
        previous = (rank, start)
        length = _number(sources[identity]["characters"])
        _check(start < end <= length)
        _check(not omitted[identity] or omitted[identity][-1][1] <= start)
        omitted[identity].append([start, end])
        _check(item["media_kind"] in {"image", "audio", "video", "binary"})
        _check(_number(item["utf8_bytes"], 4 * (end - start)) >= end - start)
        _check(
            isinstance(item["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is not None
        )
    return omitted


def validate_media_coverage(
    value: dict,
    *,
    completed: bool,
    attempts: list | None = None,
    limits: dict | None = None,
) -> None:
    """Validate opted-in restricted coverage before a review is presented or saved.

    The wire boundary invokes this only with explicit capability. Coordinates,
    complements, segment unions and closed omission metadata are checked here;
    only Core has the original source needed to recompute slice hashes/bytes.
    No media decoding, network, source mutation or inferred execution occurs.
    Malformed data raises ValueError, mapped to a safe ProviderError by the caller.
    """
    media, sources = value["media_omission"], value["sources"]
    omitted = _omissions(media, sources)
    total = delivered = missing = 0
    fully_reviewable = True
    for identity, source in sources.items():
        length = _number(source["characters"])
        ranges = _ranges(source["delivered_ranges"], length, 1025)
        _check(
            isinstance(identity, str)
            and re.fullmatch(r"task|tool-catalog|event-[0-9]{1,5}", identity)
            is not None
        )
        gaps = _ranges(source["uncovered_ranges"], length, 1026)
        _check(gaps == _complement(ranges, length))
        _check(
            type(source["fully_delivered"]) is bool
            and source["fully_delivered"] == (not gaps)
        )
        for start, end in omitted[identity]:
            _check(any(a <= start < end <= b for a, b in _union(gaps)))
        fully_reviewable &= _union(gaps) == _union(omitted[identity])
        total += length
        delivered += sum(b - a for a, b in _union(ranges))
        missing += sum(b - a for a, b in omitted[identity])
    _check(
        _number(value["total_source_characters"]) == total
        and _number(value["delivered_source_characters"]) == delivered
        and _number(media["reviewable_characters"]) == total - missing
    )
    _check(
        value["source_delivery_complete"] is False
        and type(value["all_segments_accepted"]) is bool
    )
    _check(media["reviewable_delivery_complete"] == fully_reviewable)
    _segments(value, omitted, attempts=attempts, limits=limits)
    if completed:
        _check(
            fully_reviewable
            and total > missing
            and value["all_segments_accepted"]
            and total <= (limits or {}).get("source_characters", 4194304)
        )


def _segments(
    value: dict, omitted: dict, *, attempts: list | None, limits: dict | None
) -> None:
    """Bind closed planned segments and answered calls to the physical footprint, even on stop.

    The result boundary supplies attempts and original request budgets when known.
    Historical get/export has no original request budgets, so the public maximum
    is used instead of inventing a lower budget. No accounting action is performed.
    """
    sources = value["sources"]
    segment_ranges = {key: [] for key in sources}
    calls = 0
    complete = bool(value["segments"])
    for index, segment in enumerate(value["segments"]):
        _check(
            isinstance(segment, dict)
            and set(segment) == {"id", "status", "source_ranges", "calls"}
        )
        _check(type(segment["id"]) is int and segment["id"] == index)
        _check(
            segment["status"]
            in {"not_started", "completed", "rejected", "provider_failed"}
        )
        count = _number(segment["calls"], 2)
        _check(segment["status"] != "not_started" or count == 0)
        _check(segment["status"] != "completed" or count >= 1)
        calls += count
        complete &= segment["status"] == "completed"
        _check(
            isinstance(segment["source_ranges"], list)
            and 1 <= len(segment["source_ranges"]) <= 10002
        )
        for item in segment["source_ranges"]:
            _check(isinstance(item, dict) and set(item) == {"id", "offset", "end"})
            identity = item["id"]
            _check(isinstance(identity, str) and identity in sources)
            start, end = _number(item["offset"]), _number(item["end"])
            _check(
                any(
                    a <= start < end <= b
                    for a, b in _complement(
                        omitted[identity], sources[identity]["characters"]
                    )
                )
            )
            if count:
                segment_ranges[identity].append([start, end])
    _check(value["all_segments_accepted"] is complete)
    _check(calls <= (limits or {}).get("decisions", 12))
    if attempts is not None:
        _check(isinstance(attempts, list) and len(attempts) == calls)
    _check(
        all(
            _union(segment_ranges[k]) == v["delivered_ranges"]
            for k, v in sources.items()
        )
    )


def validate_media_assessment(report: dict) -> None:
    """Reject scored or task-verdict claims in a restricted public report, not its raw audit.

    Core raw model drafts stay explicitly unconfirmed. Only the public assessment
    must carry the fixed limitation and unknown/unscored/uncited dimensions.
    """
    assessment = report["assessment"]
    _check(isinstance(assessment, dict) and assessment["outcome"] == "unknown")
    _check(
        isinstance(assessment["limitations"], list)
        and "Review scope is text and structure only; omitted media was not interpreted."
        in assessment["limitations"]
    )
    dimensions = assessment["dimensions"]
    _check(isinstance(dimensions, list) and 1 <= len(dimensions) <= 12)
    for dimension in dimensions:
        _check(
            isinstance(dimension, dict)
            and dimension["status"] == "unknown"
            and dimension["score"] is None
            and dimension["citations"] == []
        )
