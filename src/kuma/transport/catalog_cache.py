"""Bounded, credential-isolated caching of validated discovery metadata only."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..errors import KumaTimeoutError
from ..repository.strategy_groups import (
    StrategyGroupCatalog,
    is_legacy_strategy_catalog,
    validate_strategy_group_catalog,
)
from .backend import BackendClient, _wire_transport

_TTL = 60.0
_CAPACITY = 32
_LOCK = threading.Lock()
_ENTRIES: OrderedDict[tuple[str, str], tuple[float, StrategyGroupCatalog]] = (
    OrderedDict()
)


@dataclass
class _Flight:
    """Share one completed discovery attempt with its existing waiters only.

    Attributes:
        done: Signals publication of either value or error, including cancellation.
        value: Detached completed response, copied again for each waiting caller.
        error: Attempt failure propagated to current waiters, never cached by key.
    """

    done: threading.Event = field(default_factory=threading.Event)
    value: Mapping[str, Any] | None = None
    error: BaseException | None = None


_FLIGHTS: dict[tuple[str, str], _Flight] = {}


def _await_flight(flight: _Flight, timeout: float) -> Mapping[str, Any]:
    """Wait for one owned GET without starting a replacement after failure.

    Args:
        flight: Exact generation joined while holding the cache lock.
        timeout: Caller HTTP timeout; local wait is capped at 30 seconds.

    Returns:
        Detached response after the owner publishes success.

    Raises:
        KumaTimeoutError: The bounded wait elapsed; owner continues independently.
        BaseException: The original owner failure, including cancellation.

    Side Effects:
        Blocks this caller only; neither holds the cache lock nor performs I/O.
    """
    if not flight.done.wait(min(30.0, timeout)):
        raise KumaTimeoutError(
            "The KUMA metadata request timed out.",
            code="network_timeout",
            retryable=True,
        )
    if flight.error is not None:
        raise flight.error
    assert flight.value is not None
    return deepcopy(flight.value)


def _read_validated(
    fetch: Callable[[], Mapping[str, Any]],
) -> tuple[Mapping[str, Any], StrategyGroupCatalog | None]:
    """Read through the caller's existing transport and validate discovery shape.

    Args:
        fetch: Existing authenticated metadata GET boundary. It must preserve
            transport exceptions; each public caller applies its own error map
            after cache coordination, not inside the shared attempt.

    Returns:
        Detached legacy mapping with no cacheable catalog, or a closed validated
        immutable catalog and its detached wire mapping. Legacy compatibility is
        preserved but never cached.

    Raises:
        KumaError: Existing transport or canonical schema error, unchanged.

    Side Effects:
        Calls fetch once; HTTP retry ownership stays with the existing client.
    """
    raw = fetch()
    if is_legacy_strategy_catalog(raw):
        return deepcopy(raw), None
    catalog = validate_strategy_group_catalog(raw)
    return catalog.to_dict(), catalog


def read_strategy_catalog(
    backend: BackendClient | None,
    *,
    fetch: Callable[[], Mapping[str, Any]],
    refresh: bool = False,
) -> Mapping[str, Any]:
    """Read bounded discovery metadata for Run selection or explicit discovery.

    Args:
        backend: Authenticated transport owning canonical URL, credential digest
            and per-attempt timeout. Custom transports bypass caching entirely.
        fetch: Existing authenticated GET callable; preserves caller error mapping.
        refresh: Explicit discovery always starts a new network generation, even
            with an unexpired entry or another in-flight fetch.

    Returns:
        Detached catalog mapping. Only canonical immutable catalogs are cached,
        for 60 monotonic seconds from validation, across at most 32 namespaces.

    Raises:
        KumaTimeoutError: A follower waited min(30 seconds, caller timeout).
        KumaError: Fetch/validation failed; no stale entry is returned.

    Postconditions:
        New refresh generations fence older completions. Existing waiters share
        their attempt's failure without automatically starting replacement GETs.
        Run selection and capability validation still execute for each caller.

    Side Effects:
        Mutates process-local LRU and per-key flight state. No network occurs
        under the lock; no disk, paid POST, retry policy or ledger is modified.

    Security/Privacy:
        Namespace uses canonical base and exact credential digest, not raw keys.
        Custom transports never share cached values. Cached availability is not
        authorization; the service remains authoritative for every paid request.
    """
    if type(backend) is not BackendClient or backend._transport is not _wire_transport:
        return fetch()
    key = (backend.base_url, backend.credential_identity)
    with _LOCK:
        entry = _ENTRIES.get(key)
        if not refresh and entry is not None and time.monotonic() < entry[0]:
            _ENTRIES.move_to_end(key)
            return entry[1].to_dict()
        _ENTRIES.pop(key, None)
        flight = _FLIGHTS.get(key)
        owner = refresh or flight is None
        if owner:
            flight = _Flight()
            _FLIGHTS[key] = flight
    assert flight is not None
    if not owner:
        return _await_flight(flight, backend.timeout)
    try:
        value, catalog = _read_validated(fetch)
        validated_at = time.monotonic()
        with _LOCK:
            if _FLIGHTS.get(key) is flight:
                if catalog is not None:
                    _ENTRIES[key] = (validated_at + _TTL, catalog)
                    _ENTRIES.move_to_end(key)
                    while len(_ENTRIES) > _CAPACITY:
                        _ENTRIES.popitem(last=False)
                del _FLIGHTS[key]
            flight.value = value
            flight.done.set()
        return deepcopy(value)
    except BaseException as exc:
        with _LOCK:
            if _FLIGHTS.get(key) is flight:
                _ENTRIES.pop(key, None)
                del _FLIGHTS[key]
            flight.error = exc
            flight.done.set()
        raise
