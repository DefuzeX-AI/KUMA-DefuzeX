"""Bounded client-owned stage measurements; never infer remote compute latency."""

from __future__ import annotations

import copy
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def elapsed_ms(started: float | None, *, ended: float | None = None) -> int | None:
    """Measure one monotonic interval, returning unknown outside the 24-hour bound."""
    if started is None:
        return None
    duration = int(((time.monotonic() if ended is None else ended) - started) * 1000)
    return duration if 0 <= duration <= 86_400_000 else None


class StageTimeline:
    """Collect at most 200 client stages without storing arguments or error text.

    The owning Run or OfficialProvider lock serializes access. Elapsed intervals
    may overlap; callers must never sum them into invented server latency. Lost
    process-local timing is unknown after recovery, not persisted as false zeros.
    """

    def __init__(self) -> None:
        """Initialize empty stage records and an explicit overflow counter."""
        self.entries: list[dict[str, Any]] = []
        self.dropped = 0

    def record(
        self,
        stage: str,
        duration: int | None,
        *,
        status: str = "completed",
        input_id: str | None = None,
    ) -> None:
        """Append a measured stage or count overflow; names/status come from SDK literals."""
        if len(self.entries) >= 200:
            self.dropped += 1
            return
        self.entries.append(
            {
                "stage": stage,
                "elapsed_ms": duration,
                "status": status,
                "source": "client_monotonic" if duration is not None else "unavailable",
                "input_id": input_id,
            }
        )

    @contextmanager
    def measure(self, stage: str, *, input_id: str | None = None) -> Iterator[None]:
        """Measure one caller-owned interval and propagate all exceptions unchanged.

        Failures retain only a categorical status, not exception values or stack
        frames. Used around Provider requests and local Evidence preparation;
        request elapsed includes upload/response and cannot isolate server work.
        """
        started = time.monotonic()
        status = "failed"
        try:
            yield
            status = "completed"
        finally:
            self.record(stage, elapsed_ms(started), status=status, input_id=input_id)

    def snapshot(self) -> dict[str, Any]:
        """Detach bounded records; unavailable remote sub-stages remain explicit null."""
        return {"stages": copy.deepcopy(self.entries), "dropped_stages": self.dropped}
