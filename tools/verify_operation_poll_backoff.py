"""Offline regressions for operation poll backoff and mid-flight poll_after_ms."""

from __future__ import annotations

import unittest
from collections.abc import Callable, Mapping
from typing import Any
from unittest.mock import patch

from kuma.errors import KumaTimeoutError, ProviderError, ServiceError
from kuma.transport import operations as ops
from kuma.transport.backend import BackendClient
from kuma.transport.http import WireResponse
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


class RealBackendDeadlineTests(unittest.TestCase):
    """Use the real request timeout/retry path, replacing only the HTTP wire."""

    def drive(
        self,
        *,
        complete_at=77.4,
        budget=120.0,
        interval=None,
        failures=(),
        max_retries=0,
        latency=0.0,
        store=None,
        clock=None,
    ):
        clock = clock or _Clock()
        store = store or _store()
        calls = []
        posts = []
        deadline = clock.now + budget

        def wire(method, _url, headers, _body, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, deadline - clock.now + 1e-9)
            if method == "POST":
                posts.append(headers)
                return WireResponse(202, _start("unused", deadline))
            calls.append((clock.now, timeout))
            if len(calls) in failures:
                raise ServiceError(
                    "Synthetic transport failure", code="network_error", retryable=True
                )
            if latency:
                clock.now += min(latency, timeout)
                if latency >= timeout:
                    raise KumaTimeoutError(
                        "Synthetic HTTP timeout", code="network_timeout", retryable=True
                    )
            response = {"operation_id": OPERATION_ID, "status": "running"}
            if clock.now >= complete_at:
                response.update(status="succeeded", result={"ok": True})
            elif interval is not None:
                response["poll_after_ms"] = interval
            return WireResponse(200, response)

        client = BackendClient(
            "dfx_test",
            base_url="https://example.test",
            transport=wire,
            max_retries=max_retries,
        )
        error = None
        result = None
        with (
            patch.object(ops.time, "sleep", clock.sleep),
            patch.object(ops.time, "monotonic", clock.monotonic),
            patch("kuma.transport.backend.schedule_update_check"),
        ):
            try:
                result = await_operation(
                    client,
                    store,
                    key_factory=lambda: "repro-stable-key",
                    start=lambda key, end: client.json(
                        "POST",
                        "/sdk/v2/judge/",
                        {},
                        idempotency_key=key,
                        _deadline=end,
                        _expected_status=202,
                    ),
                    wait_timeout=budget,
                )
            except KumaTimeoutError as exc:
                error = exc
        return clock, calls, posts, store, result, error

    def test_original_completion_and_deadline_gap_use_real_transport(self):
        for ready, budget, observed in ((77.4, 120, 79.5), (89, 90, 89.9)):
            clock, calls, posts, store, result, error = self.drive(
                complete_at=ready, budget=budget
            )
            self.assertIsNone(error)
            self.assertEqual(result, {"ok": True})
            self.assertAlmostEqual(clock.now, observed)
            self.assertLessEqual(len(calls), 16)
            self.assertEqual(len(posts), 1)
            self.assertIsNone(store.load())
            self.assertTrue(all(t < budget for t, _ in calls))

    def test_exhaustion_preserves_identity_and_resume_get_only(self):
        clock, calls, posts, store, result, error = self.drive(
            complete_at=999, budget=90
        )
        self.assertEqual(error.code, "operation_wait_timeout")
        self.assertTrue(error.retryable)
        self.assertIsNone(result)
        self.assertAlmostEqual(clock.now, 90)
        self.assertAlmostEqual(calls[-1][0], 89.9)
        self.assertEqual(len(posts), 1)
        state = store.load()
        self.assertEqual(state.operation_id, OPERATION_ID)
        self.assertEqual(state.idempotency_key, "repro-stable-key")
        resumed = self.drive(complete_at=89, budget=1, store=store, clock=clock)
        self.assertEqual(resumed[2], [])
        self.assertEqual(resumed[4], {"ok": True})

    def test_explicit_interval_deadline_and_small_remaining_budget(self):
        clock, calls, _, _, result, error = self.drive(
            complete_at=89, budget=90, interval=60_000
        )
        self.assertIsNone(error)
        self.assertEqual(result, {"ok": True})
        self.assertEqual([t for t, _ in calls], [0, 60, 89.9])
        clock, calls, _, _, _, error = self.drive(complete_at=999, budget=0.05)
        self.assertEqual(error.code, "operation_wait_timeout")
        self.assertEqual([t for t, _ in calls], [0, 0.025])
        self.assertEqual(clock.now, 0.05)

    def test_transient_http_retry_and_outer_poll_retry_remain_bounded(self):
        for retries in (0, 1):
            clock, calls, posts, _, result, error = self.drive(
                complete_at=1, failures=(1,), max_retries=retries
            )
            self.assertIsNone(error)
            self.assertEqual(result, {"ok": True})
            self.assertEqual(len(posts), 1)
            self.assertGreater(calls[1][0], calls[0][0])
            self.assertLess(clock.now, 120)
        clock, calls, _, store, _, error = self.drive(
            complete_at=999, budget=0.05, failures=(2,), max_retries=2
        )
        self.assertEqual(error.code, "operation_wait_timeout")
        self.assertEqual(len(calls), 2)
        self.assertEqual(clock.now, 0.05)
        self.assertIsNotNone(store.load())

    def test_network_latency_can_exhaust_reserved_budget_without_extension(self):
        clock, calls, _, store, _, error = self.drive(
            complete_at=89, budget=90, latency=0.2
        )
        self.assertEqual(error.code, "operation_wait_timeout")
        self.assertLessEqual(clock.now, 90)
        self.assertGreater(calls[-1][1], 0)
        self.assertLessEqual(calls[-1][1], 0.101)
        self.assertIsNotNone(store.load())

    def test_exhausted_start_budget_does_not_send_get(self):
        clock = _Clock()
        client = BackendClient(
            "dfx_test",
            base_url="https://example.test",
            transport=lambda *_: self.fail("expired GET"),
        )
        store = _store()

        def start(key, deadline):
            clock.now = deadline
            return _start(key, deadline)

        with (
            patch.object(ops.time, "monotonic", clock.monotonic),
            self.assertRaises(KumaTimeoutError),
        ):
            await_operation(
                client,
                store,
                key_factory=lambda: "same-key",
                start=start,
                wait_timeout=1,
            )
        self.assertEqual(store.load().operation_id, OPERATION_ID)

    def test_invalid_start_status_is_rejected(self):
        with self.assertRaises(ProviderError):
            ops._start_response({**_start("key", 1), "status": "unknown"})

    def test_late_response_and_scheduler_oversleep_keep_pending(self):
        for late_response in (False, True):
            clock = _Clock()
            calls = []
            store = _store()

            def wire(*_args, calls=calls, clock=clock, late_response=late_response):
                calls.append(clock.now)
                self.assertLess(clock.now, 1)
                if late_response:
                    clock.now = 1.01
                    return WireResponse(
                        200,
                        {
                            "operation_id": OPERATION_ID,
                            "status": "succeeded",
                            "result": {"ok": True},
                        },
                    )
                return WireResponse(
                    200, {"operation_id": OPERATION_ID, "status": "running"}
                )

            def oversleep(seconds, clock=clock):
                clock.now += seconds + 1

            client = BackendClient(
                "dfx_test", base_url="https://example.test", transport=wire
            )
            with (
                patch.object(ops.time, "monotonic", clock.monotonic),
                patch.object(ops.time, "sleep", oversleep),
                patch("kuma.transport.backend.schedule_update_check"),
                self.assertRaises(KumaTimeoutError),
            ):
                await_operation(
                    client,
                    store,
                    key_factory=lambda: "key",
                    start=_start,
                    wait_timeout=1,
                )
            self.assertEqual(len(calls), 1)
            self.assertEqual(store.load().operation_id, OPERATION_ID)


if __name__ == "__main__":
    unittest.main()
