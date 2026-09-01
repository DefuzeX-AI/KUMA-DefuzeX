"""Bounded conversion for JSON values accepted by public SDK contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

MAX_JSON_CONTAINER_DEPTH = 256


class JsonStructureError(ValueError):
    """Report an invalid JSON graph without retaining or displaying its values."""


def _transform_json(
    value: Any,
    *,
    freeze: bool,
    depth: int,
    active_containers: set[int],
) -> Any:
    """Copy one JSON graph while enforcing finite scalars, depth, and acyclicity.

    Args:
        value: Candidate scalar or container. Mappings and ``list``/``tuple``
            containers are supported; other Sequence implementations retain
            their existing unsupported-object behavior.
        freeze: Return read-only mappings and tuples when ``True``; otherwise
            return ordinary detached dictionaries and lists.
        depth: Number of containers on the current ancestor path. The root
            container therefore enters at depth one.
        active_containers: Object identities on the current traversal path.
            Identities are removed when their container finishes, so a shared
            child used by two siblings is not mistaken for a cycle.

    Returns:
        A detached value containing only finite JSON scalar types and built-in
        containers, optionally frozen.

    Raises:
        JsonStructureError: If a value is unsupported, non-finite, cyclic, or
            deeper than :data:`MAX_JSON_CONTAINER_DEPTH`.
        Exception: A custom Mapping may raise while exposing its items. Public
            SDK boundaries catch and replace such failures with stable errors.

    Preconditions:
        ``active_containers`` belongs only to this traversal. Callers must not
        reuse it across independent values.

    Postconditions:
        Success shares no mutable container with ``value``. Failure retains no
        caller value or object representation in the raised message.

    Side Effects:
        Iterates custom Mapping objects and calls ``str`` on their keys, matching
        the SDK's established mapping-to-JSON behavior. It performs no I/O.

    Security/Privacy:
        Error text contains only a stable structural reason, never a key,
        scalar value, object representation, or host detail.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise JsonStructureError("non-finite JSON number")
    if not isinstance(value, (Mapping, list, tuple)):
        raise JsonStructureError("unsupported JSON value")
    if depth >= MAX_JSON_CONTAINER_DEPTH:
        raise JsonStructureError("JSON container depth exceeded")

    identity = id(value)
    if identity in active_containers:
        raise JsonStructureError("cyclic JSON value")
    active_containers.add(identity)
    try:
        if isinstance(value, Mapping):
            transformed = {
                str(key): _transform_json(
                    child,
                    freeze=freeze,
                    depth=depth + 1,
                    active_containers=active_containers,
                )
                for key, child in value.items()
            }
            return MappingProxyType(transformed) if freeze else transformed
        transformed_items = tuple(
            _transform_json(
                child,
                freeze=freeze,
                depth=depth + 1,
                active_containers=active_containers,
            )
            for child in value
        )
        return transformed_items if freeze else list(transformed_items)
    finally:
        active_containers.remove(identity)


def detach_json(value: Any) -> Any:
    """Return a mutable JSON copy after bounded graph and encoder validation.

    Args:
        value: Candidate public payload. Mappings and ``list``/``tuple`` values
            may contain at most 256 nested containers, counting a container at
            the root as level one.

    Returns:
        A detached graph made only of dictionaries, lists, and finite JSON
        scalars. Reused but acyclic child containers are copied independently.

    Raises:
        JsonStructureError: If the graph is cyclic, too deep, non-finite,
            unsupported, or rejected by Python's JSON encoder.
        Exception: A custom Mapping may fail while being inspected. SDK caller
            boundaries convert this to their stable public error type.

    Postconditions:
        Successful output can be serialized with ``allow_nan=False`` and cannot
        be mutated through a caller-owned container.

    Side Effects:
        Iterates custom Mapping values once; performs no persistence, Evidence,
        network, model, or billing operation.

    Security/Privacy:
        Structural failure messages never include caller data.
    """
    try:
        plain = _transform_json(
            value,
            freeze=False,
            depth=0,
            active_containers=set(),
        )
        json.dumps(plain, allow_nan=False)
    except JsonStructureError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise JsonStructureError("invalid JSON value") from exc
    return plain


def freeze_json(value: Any) -> Any:
    """Return a recursively immutable copy of one bounded JSON value.

    Args:
        value: Candidate public contract field following the same accepted
            shapes and 256-container depth limit as :func:`detach_json`.

    Returns:
        Finite scalars unchanged, mappings as ``MappingProxyType``, and arrays
        as tuples. Shared aliases remain valid but become detached copies.

    Raises:
        JsonStructureError: If ``value`` is cyclic, too deep, non-finite, or not
            JSON compatible.
        Exception: A custom Mapping may fail while being inspected. Contract
            constructors replace this with a stable SDK validation error.

    Postconditions:
        Success cannot be changed through the source containers and is safe for
        immutable Input, Submission, Evidence, and report contracts.

    Side Effects:
        Iterates a custom Mapping once during detachment. No external I/O or SDK
        lifecycle state is touched.
    """
    plain = detach_json(value)
    return _transform_json(
        plain,
        freeze=True,
        depth=0,
        active_containers=set(),
    )


__all__ = [
    "MAX_JSON_CONTAINER_DEPTH",
    "JsonStructureError",
    "detach_json",
    "freeze_json",
]
