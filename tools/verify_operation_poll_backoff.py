"""Offline regressions for operation poll backoff and mid-flight poll_after_ms."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Mapping
from typing import Any
from unittest.mock import patch

from kuma.errors import ProviderError
from kuma.transport import operations as ops
from kuma.transport.operations import PendingOperationStore, await_operation

OPERATION_ID = "0999c499-81d9-4228-9dac-a9ce4ce4cbc7"
BACKEND_POLL_AFTER_MS = 500
LONG_OP_SECONDS = 77.4
MAX_POLL_SECONDS = 60.0


class _Clock:
    """Advance monotonic time only when the poller sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class StubClient:
    """Return scripted JSON mappings for GET operation polls."""

    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        self.responses = responses
        self.calls = 0

    def json(self, method: str, path: str, **_kwargs: Any) -> Mapping[str, Any]:
        del method, path
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


class TimedStubClient:
    """Return running until the patched clock reaches the captured Judge span."""

    def __init__(self, clock: _Clock, *, revise_ms: int | None = None) -> None:
        self.clock = clock
        self.revise_ms = revise_ms
        self.calls = 0

    def json(self, method: str, path: str, **_kwargs: Any) -> Mapping[str, Any]:
        del method, path
        self.calls += 1
        if self.clock.now >= LONG_OP_SECONDS:
            return {
                "operation_id": OPERATION_ID,
                "status": "succeeded",
                "result": {"ok": True},
            }
        response: dict[str, Any] = {
            "operation_id": OPERATION_ID,
            "status": "running",
        }
        if self.revise_ms is not None:
            response["poll_after_ms"] = self.revise_ms
        return response


def _start(_key: str, _deadline: float) -> Mapping[str, Any]:
    return {
        "operation_id": OPERATION_ID,
        "status": "queued",
        "poll_after_ms": BACKEND_POLL_AFTER_MS,
    }


def _store() -> PendingOperationStore:
    return PendingOperationStore(
        None,
        operation_type="judge",
        base_url="https://defuzex.ai/api/agentdefuze",
    )


def _expected_backoff(start_ms: int, sleeps: int) -> list[float]:
    current = start_ms
    values: list[float] = []
    for _ in range(sleeps):
        values.append(current / 1_000)
        current = min(current * 2, 60_000)
    return values


class OperationPollBackoffTests(unittest.TestCase):
    def test_issue_89_shapes_grow_instead_of_fixed_500ms(self) -> None:
        running = {"operation_id": OPERATION_ID, "status": "running"}
        done = {
            "operation_id": OPERATION_ID,
            "status": "succeeded",
            "result": {"ok": True},
        }
        slept, calls, error = self._drive([running] * 69 + [done])
        self.assertIsNone(error)
        self.assertEqual(calls, 70)
        self.assertEqual(slept, _expected_backoff(BACKEND_POLL_AFTER_MS, 69))
        self.assertGreater(slept[1], slept[0])
        self.assertEqual(slept[-1], MAX_POLL_SECONDS)
        self.assertNotEqual(set(slept), {0.5})

    def test_mid_flight_poll_after_ms_is_accepted_and_used(self) -> None:
        slower = {
            "operation_id": OPERATION_ID,
            "status": "running",
            "poll_after_ms": 30_000,
        }
        done = {
            "operation_id": OPERATION_ID,
            "status": "succeeded",
            "result": {"ok": True},
        }
        slept, calls, error = self._drive([slower, slower, done])
        self.assertIsNone(error)
        self.assertEqual(calls, 3)
        self.assertEqual(slept, [30.0, 30.0])

    def test_long_operation_poll_count_drops_via_backoff(self) -> None:
        clock = _Clock()
        client = TimedStubClient(clock)
        with (
            patch.object(ops.time, "sleep", clock.sleep),
            patch.object(ops.time, "monotonic", clock.monotonic),
        ):
            result = await_operation(
                client,
                _store(),
                key_factory=lambda: "repro-stable-key",
                start=_start,
                wait_timeout=600.0,
            )
        self.assertEqual(result, {"ok": True})
        self.assertLessEqual(client.calls, 13)
        self.assertGreater(client.calls, 1)
        self.assertEqual(
            clock.slept, _expected_backoff(BACKEND_POLL_AFTER_MS, len(clock.slept))
        )
        self.assertLess(client.calls, 70)

    def test_backend_revision_also_reduces_poll_count(self) -> None:
        clock = _Clock()
        client = TimedStubClient(clock, revise_ms=30_000)
        with (
            patch.object(ops.time, "sleep", clock.sleep),
            patch.object(ops.time, "monotonic", clock.monotonic),
        ):
            result = await_operation(
                client,
                _store(),
                key_factory=lambda: "repro-stable-key",
                start=_start,
                wait_timeout=600.0,
            )
        self.assertEqual(result, {"ok": True})
        self.assertLessEqual(client.calls, 6)
        self.assertEqual(clock.slept, [30.0] * len(clock.slept))

    def test_resume_learns_poll_after_ms_instead_of_1000ms(self) -> None:
        store = _store()
        state = store.load_or_create(lambda: "repro-stable-key")
        store.set_operation_id(state, OPERATION_ID)
        responses = [
            {
                "operation_id": OPERATION_ID,
                "status": "running",
                "poll_after_ms": 5_000,
            },
            {"operation_id": OPERATION_ID, "status": "running"},
            {
                "operation_id": OPERATION_ID,
                "status": "succeeded",
                "result": {"ok": True},
            },
        ]
        slept, calls, error = self._drive(
            responses, store=store, start=lambda *_args: self.fail("start")
        )
        self.assertIsNone(error)
        self.assertEqual(calls, 3)
        self.assertEqual(slept, [5.0, 10.0])

    def test_resume_without_revision_grows_from_default(self) -> None:
        store = _store()
        state = store.load_or_create(lambda: "repro-stable-key")
        store.set_operation_id(state, OPERATION_ID)
        responses = [
            {"operation_id": OPERATION_ID, "status": "running"},
            {"operation_id": OPERATION_ID, "status": "running"},
            {
                "operation_id": OPERATION_ID,
                "status": "succeeded",
                "result": {"ok": True},
            },
        ]
        slept, calls, error = self._drive(
            responses, store=store, start=lambda *_args: self.fail("start")
        )
        self.assertIsNone(error)
        self.assertEqual(calls, 3)
        self.assertEqual(slept, [1.0, 2.0])

    def test_unknown_active_field_is_still_invalid(self) -> None:
        _, calls, error = self._drive(
            [
                {
                    "operation_id": OPERATION_ID,
                    "status": "running",
                    "poll_after_ms": 1_000,
                    "hint": 1,
                }
            ]
        )
        self.assertEqual(calls, 1)
        self.assertIsInstance(error, ProviderError)
        self.assertEqual(error.code, "invalid_response")

    def test_out_of_range_mid_flight_interval_is_invalid(self) -> None:
        _, calls, error = self._drive(
            [
                {
                    "operation_id": OPERATION_ID,
                    "status": "queued",
                    "poll_after_ms": 70_000,
                }
            ]
        )
        self.assertEqual(calls, 1)
        self.assertIsInstance(error, ProviderError)
        self.assertEqual(error.code, "invalid_response")

    def _drive(
        self,
        responses: list[Mapping[str, Any]],
        *,
        store: PendingOperationStore | None = None,
        start: Callable[[str, float], Mapping[str, Any]] = _start,
    ) -> tuple[list[float], int, Exception | None]:
        slept: list[float] = []
        client = StubClient(responses)
        error: Exception | None = None
        with patch.object(ops.time, "sleep", lambda seconds: slept.append(seconds)):
            try:
                await_operation(
                    client,
                    _store() if store is None else store,
                    key_factory=lambda: "repro-stable-key",
                    start=start,
                    wait_timeout=600.0,
                )
            except Exception as exc:
                error = exc
        return slept, client.calls, error


if __name__ == "__main__":
    unittest.main()
