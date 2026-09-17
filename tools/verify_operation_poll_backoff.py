"""Offline regressions for operation poll backoff and mid-flight poll_after_ms."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Mapping
from typing import Any
from unittest.mock import patch

from kuma.errors import KumaTimeoutError, ProviderError
from kuma.transport import operations as ops
from kuma.transport.operations import PendingOperationStore, await_operation

OPERATION_ID = "0999c499-81d9-4228-9dac-a9ce4ce4cbc7"
BACKEND_POLL_AFTER_MS = 500
LONG_OP_SECONDS = 77.4
DEADLINE_SECONDS = 120.0
MAX_SERVER_POLL_SECONDS = 60.0
MAX_FALLBACK_POLL_SECONDS = ops._MAX_FALLBACK_POLL_MS / 1_000


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

    def __init__(
        self,
        clock: _Clock,
        *,
        revise_ms: int | None = None,
        complete_at: float = LONG_OP_SECONDS,
    ) -> None:
        self.clock = clock
        self.revise_ms = revise_ms
        self.complete_at = complete_at
        self.calls = 0

    def json(self, method: str, path: str, **_kwargs: Any) -> Mapping[str, Any]:
        del method, path
        self.calls += 1
        if self.clock.now >= self.complete_at:
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
        current = min(current * 2, ops._MAX_FALLBACK_POLL_MS)
    return values


def _await_with_clock(
    client: TimedStubClient,
    clock: _Clock,
    *,
    store: PendingOperationStore | None = None,
    wait_timeout: float,
    start: Callable[[str, float], Mapping[str, Any]] = _start,
) -> Mapping[str, Any]:
    with (
        patch.object(ops.time, "sleep", clock.sleep),
        patch.object(ops.time, "monotonic", clock.monotonic),
    ):
        return await_operation(
            client,
            _store() if store is None else store,
            key_factory=lambda: "repro-stable-key",
            start=start,
            wait_timeout=wait_timeout,
        )


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
        self.assertEqual(slept[-1], MAX_FALLBACK_POLL_SECONDS)
        self.assertLess(slept[-1], MAX_SERVER_POLL_SECONDS)
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
        result = _await_with_clock(client, clock, wait_timeout=600.0)
        self.assertEqual(result, {"ok": True})
        self.assertLessEqual(client.calls, 15)
        self.assertGreater(client.calls, 1)
        self.assertEqual(
            clock.slept, _expected_backoff(BACKEND_POLL_AFTER_MS, len(clock.slept))
        )
        self.assertLess(client.calls, 70)
        self.assertGreaterEqual(clock.now, LONG_OP_SECONDS)
        self.assertLess(clock.now, DEADLINE_SECONDS)

    def test_long_operation_is_observed_before_120s_deadline(self) -> None:
        clock = _Clock()
        client = TimedStubClient(clock)
        result = _await_with_clock(client, clock, wait_timeout=DEADLINE_SECONDS)
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(clock.now, LONG_OP_SECONDS)
        self.assertLess(clock.now, DEADLINE_SECONDS)
        self.assertLess(clock.now - LONG_OP_SECONDS, MAX_FALLBACK_POLL_SECONDS)
        self.assertEqual(
            clock.slept, _expected_backoff(BACKEND_POLL_AFTER_MS, len(clock.slept))
        )
        self.assertLessEqual(client.calls, 15)

    def test_deadline_sleep_still_collects_completed_result(self) -> None:
        clock = _Clock()
        client = TimedStubClient(clock, complete_at=89.0)
        result = _await_with_clock(client, clock, wait_timeout=90.0)
        self.assertEqual(result, {"ok": True})
        self.assertGreaterEqual(clock.now, 89.0)
        self.assertLessEqual(clock.now, 90.0)
        self.assertGreater(client.calls, 1)

    def test_backend_revision_also_reduces_poll_count(self) -> None:
        clock = _Clock()
        client = TimedStubClient(clock, revise_ms=30_000)
        result = _await_with_clock(client, clock, wait_timeout=600.0)
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
        self.assertEqual(slept, [5.0, MAX_FALLBACK_POLL_SECONDS])

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

    def test_invalid_optional_interval_types_and_bounds_are_rejected(self) -> None:
        invalid_values: tuple[Any, ...] = (
            True,
            False,
            500.0,
            "500",
            None,
            0,
            -1,
            99,
            60_001,
        )
        for value in invalid_values:
            with self.subTest(poll_after_ms=value):
                _, calls, error = self._drive(
                    [
                        {
                            "operation_id": OPERATION_ID,
                            "status": "running",
                            "poll_after_ms": value,
                        }
                    ]
                )
                self.assertEqual(calls, 1)
                self.assertIsInstance(error, ProviderError)
                self.assertEqual(error.code, "invalid_response")

    def test_documented_optional_interval_bounds_are_accepted(self) -> None:
        done = {
            "operation_id": OPERATION_ID,
            "status": "succeeded",
            "result": {"ok": True},
        }
        for value, expected_sleep in ((100, 0.1), (60_000, 60.0)):
            with self.subTest(poll_after_ms=value):
                slept, calls, error = self._drive(
                    [
                        {
                            "operation_id": OPERATION_ID,
                            "status": "running",
                            "poll_after_ms": value,
                        },
                        done,
                    ]
                )
                self.assertIsNone(error)
                self.assertEqual(calls, 2)
                self.assertEqual(slept, [expected_sleep])

    def test_timeout_retains_recovery_metadata(self) -> None:
        clock = _Clock()
        store = _store()
        client = TimedStubClient(clock)
        with self.assertRaises(KumaTimeoutError) as raised:
            _await_with_clock(
                client, clock, store=store, wait_timeout=DEADLINE_SECONDS / 4
            )
        error = raised.exception
        self.assertEqual(error.code, "operation_wait_timeout")
        self.assertTrue(error.retryable)
        state = store.load()
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.operation_id, OPERATION_ID)
        self.assertEqual(state.idempotency_key, "repro-stable-key")
        self.assertGreater(client.calls, 1)
        self.assertLess(clock.now, LONG_OP_SECONDS)

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
