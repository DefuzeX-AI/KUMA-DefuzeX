"""Backward-compatible public client backed by the v4 HTTP boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import observation_contract as _observation
from .config import resolve_api_key
from .errors import (
    AuthenticationError,
    LimitExceededError,
    PermissionDeniedError,
)
from .exceptions import (
    KumaAuthenticationError,
    KumaPermissionError,
    KumaRateLimitError,
)
from .repository.strategy_groups import (
    StrategyGroupCatalog,
    validate_strategy_group_catalog,
)
from .transport.backend import (
    DEFAULT_BASE_URL,
    BackendClient,
    WireTransport,
    _validate_base_url,
    _validate_timeout,
)
from .transport.catalog_cache import read_strategy_catalog

Transport = WireTransport


class KumaClient:
    """Read service configuration and explicitly manage cloud observations.

    Args:
        api_key: Optional opaque ``dfx_`` credential. ``None`` resolves
            ``KUMA_API_KEY`` and then the local credential file. Construction may
            remain unauthenticated, but read methods then raise an authentication
            error.
        base_url: Public Backend API base URL. Remote URLs require HTTPS;
            loopback HTTP is accepted for local integration testing.
        timeout: Positive finite timeout in seconds for each HTTP request.
        transport: Optional boundary callable used by deterministic integration
            tests. Ordinary users should leave it as ``None``.

    Raises:
        ConfigurationError: If URL, timeout, or a discovered credential is invalid.

    Preconditions:
        A remote ``base_url`` uses HTTPS. A supplied/discovered key satisfies the
        KUMA format; omitting a key is allowed only until an authenticated read.

    Postconditions:
        The reusable client holds validated URL, timeout, and credential state.
        Construction makes no request and does not prove server acceptance.

    Side Effects:
        May read ``KUMA_API_KEY`` or the user credential file. Public read
        methods each perform one Backend GET request without retry.

    Security/Privacy:
        ``repr`` exposes only URL and whether a key exists, never its value. This
        client does not contact MCP, model providers, or databases directly.
        Observation upload/delete occurs only through explicit method calls;
        local observation never uploads automatically. Cloud history is owned
        by the authenticated user, shared across that user's authorized keys.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        """Validate configuration and prepare credential-optional public reads.

        Args:
            api_key: Optional explicit opaque credential; see the class contract.
            base_url: Public Backend base URL.
            timeout: Per-request deadline in seconds.
            transport: Optional HTTP boundary replacement for tests.

        Raises:
            ConfigurationError: If any configuration value is invalid.

        Postconditions:
            An authenticated internal Backend client exists only when a key was
            resolved. No HTTP request has occurred.
        """
        self.base_url = _validate_base_url(base_url)
        self.timeout = _validate_timeout(timeout)
        resolved_key = resolve_api_key(api_key, required=False)
        self._authenticated = resolved_key is not None
        self._backend = (
            None
            if resolved_key is None
            else BackendClient(
                resolved_key,
                base_url=self.base_url,
                timeout=self.timeout,
                transport=transport,
                max_retries=0,
            )
        )

    def __repr__(self) -> str:
        """Return a credential-safe diagnostic representation.

        Returns:
            Text containing the public base URL and authentication-presence flag.

        Security/Privacy:
            The API key and credential path are never included.
        """
        return (
            f"KumaClient(base_url={self.base_url!r}, "
            f"authenticated={self._authenticated})"
        )

    def _read(self, path: str) -> Mapping[str, Any]:
        """Perform one public configuration GET and map legacy client errors.

        Args:
            path: Absolute SDK API route relative to ``base_url``.

        Returns:
            Decoded JSON object returned by the public Backend.

        Raises:
            KumaAuthenticationError: If no key is configured or authentication
                is rejected.
            KumaPermissionError: If the key lacks the required scope.
            KumaRateLimitError: If the account quota is exhausted.
            KumaError: For other validated public transport failures.

        Preconditions:
            ``path`` is a fixed SDK route chosen by a public method, not an
            arbitrary caller URL.

        Side Effects:
            Performs one authenticated HTTPS/allowed-loopback GET request.
            Strategy discovery refreshes the shared catalog cache before this
            entry point maps transport errors; Run waiters retain their own
            original SDK error classes rather than this client's legacy mapping.

        Security/Privacy:
            Stable public errors are exposed without returning raw remote bodies.
        """
        if self._backend is None:
            raise KumaAuthenticationError(
                401, "Set KUMA_API_KEY or pass api_key to KumaClient."
            )
        try:
            if path == "/sdk/strategies/":
                return read_strategy_catalog(
                    self._backend,
                    fetch=lambda: self._backend.json("GET", path),
                    refresh=True,
                )
            return self._backend.json("GET", path)
        except AuthenticationError as exc:
            raise KumaAuthenticationError(401, str(exc)) from None
        except PermissionDeniedError as exc:
            raise KumaPermissionError(403, str(exc)) from None
        except LimitExceededError:
            raise KumaRateLimitError(
                429, "The KUMA account quota has been exhausted."
            ) from None

    def entitlements(self) -> Mapping[str, Any]:
        """Fetch public user, key-scope, subscription, and quota information.

        Returns:
            Validated JSON mapping from ``/sdk/entitlements/``. Callers should
            treat unknown forward-compatible fields as data.

        Raises:
            KumaAuthenticationError: No usable key or rejected authentication.
            KumaPermissionError: Key cannot read entitlements.
            KumaRateLimitError: Account quota prevents the read.

        Side Effects:
            Performs one public Backend GET request.
        """

        return self._read("/sdk/entitlements/")

    def _observation_request(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        """Perform one explicit storage HTTP call using existing auth/timeout rules.

        No retries, polling, billing workflow or local persistence are introduced.
        Standard KumaError classes retain stable server codes such as capacity,
        disabled service and conflict, rather than account-read legacy remapping.
        """
        if self._backend is None:
            raise AuthenticationError("Set KUMA_API_KEY or pass api_key to KumaClient.")
        return self._backend.json(
            method,
            path,
            payload,
            idempotency_key=payload["observation_id"] if method == "POST" else None,
            _expected_status=200,
        )

    def upload_observation(self, observation: Mapping[str, Any]) -> Mapping[str, Any]:
        """Explicitly store a completed local export, without evaluating the Agent.

        Args:
            observation: Closed ``kuma.observation.v1`` from session.export();
                finite, already sanitized, at most five MiB canonical JSON.
        Returns:
            Detached closed receipt binding the exact observation ID/hash/bytes.
        Raises:
            ValidationError: Invalid/sensitive/oversize input, before any HTTP.
            ProviderError: Malformed or mismatched server receipt.
            KumaError: Auth, sdk:observe scope, conflict, capacity or HTTP failure.
        Preconditions:
            Cloud observation is enabled; the key explicitly has sdk:observe.
        Postconditions:
            Same owner/ID/content retries reuse storage. Changed content conflicts;
            failure never modifies local capture, Agent outcome or evaluation.
        Side Effects:
            One authenticated POST. No Case/Judge/model, credit, local save or
            automatic retry. Users may explicitly retry the unchanged export.
        Security/Privacy:
            Upload is opt-in. No tenant/user selectors or credentials in the body;
            other authorized keys of the same user can read retained content.
        """
        body = _observation.checked(_observation.export, observation)
        response = self._observation_request("POST", "/sdk/observations/", body)
        return _observation.checked(
            lambda v: _observation.receipt(v, body=body), response, remote=True
        )

    def list_observations(
        self, *, limit: int = 20, cursor: str | None = None
    ) -> Mapping[str, Any]:
        """Read one bounded metadata-only history page for the authenticated user.

        Args:
            limit: Strict integer 1-100; defaults to 20, no automatic pagination.
            cursor: Prior page's opaque obs_ ID, or None for the newest page.
        Returns:
            Detached kuma.observation_list.v1 items and nullable next_cursor;
            no span/tool/model bodies. Each item includes receipt and statuses.
        Raises:
            ValidationError: Invalid limit/cursor before HTTP.
            ProviderError: Malformed/private-shaped page.
            KumaError: Missing sdk:read, unavailable service or transport failure.
        Side Effects:
            One authenticated GET. No local writes or evaluation; history belongs
            to the user/tenant, not exclusively to this key.
        """
        count = _observation.checked(_observation.page_limit, limit)
        path = f"/sdk/observations/?limit={count}"
        if cursor is not None:
            path += "&cursor=" + _observation.checked(_observation.identifier, cursor)
        response = self._observation_request("GET", path)
        return _observation.checked(
            lambda v: _observation.page(v, count), response, remote=True
        )

    def get_observation(self, observation_id: str) -> Mapping[str, Any]:
        """Retrieve an owned capture body only on an explicit authenticated read.

        Args:
            observation_id: Exact obs_ plus 32 lowercase hex characters from a receipt.
        Returns:
            Detached closed detail containing receipt and sanitized full export.
        Raises:
            ValidationError: Invalid ID before HTTP.
            ProviderError: Invalid shape, privacy, hash or requested-ID binding.
            KumaError: Missing sdk:read, unknown/foreign ID or HTTP failure.
        Side Effects:
            One GET; no file saved. The returned body may include private Agent
            context after recognized-secret redaction; share/export deliberately.
        """
        identifier = _observation.checked(_observation.identifier, observation_id)
        response = self._observation_request("GET", f"/sdk/observations/{identifier}/")
        return _observation.checked(
            lambda v: _observation.detail(v, identifier), response, remote=True
        )

    def delete_observation(self, observation_id: str) -> Mapping[str, Any]:
        """Explicitly delete cloud storage for one observation; local copies stay.

        Args:
            observation_id: Exact obs_ plus 32 lowercase hex characters.
        Returns:
            Closed deletion acknowledgment, including already absent IDs. This
            does not disclose whether another user owns the requested ID.
        Raises:
            ValidationError: Invalid ID before HTTP.
            ProviderError: Invalid acknowledgment.
            KumaError: Missing explicit sdk:observe scope or HTTP failure.
        Side Effects:
            One DELETE, atomically reclaiming owned storage. No billing/evaluation.
            Re-upload after deletion creates a new stored record, not a replay.
        """
        identifier = _observation.checked(_observation.identifier, observation_id)
        response = self._observation_request(
            "DELETE", f"/sdk/observations/{identifier}/"
        )
        return _observation.checked(
            lambda v: _observation.deleted(v, identifier), response, remote=True
        )

    def strategies(self) -> Mapping[str, Any]:
        """Fetch the public active Case strategy catalog for explicit discovery.

        Returns:
            Backend-managed strategy mapping available to this credential.

        Raises:
            KumaAuthenticationError: No usable key or rejected authentication.
            KumaPermissionError: Key cannot read strategy configuration.
            KumaRateLimitError: Account quota prevents the read.

        Side Effects:
            Always refreshes through a public Backend GET, replacing the shared
            process-local discovery cache; failure invalidates the old entry.
            Run selection uses the cache but repeats selection/capability checks.
        """

        return self._read("/sdk/strategies/")

    def strategy_group_catalog(self) -> StrategyGroupCatalog:
        """Fetch and validate the versioned public Strategy Group catalog.

        Returns:
            Immutable catalog with exact group coordinates, capability
            capabilities, limits, availability, and semantic default.

        Raises:
            KumaAuthenticationError: No usable key or rejected authentication.
            KumaPermissionError: Key cannot read strategy configuration.
            KumaRateLimitError: Account quota prevents the read.
            ValidationError: The service returned a legacy or malformed catalog.

        Side Effects:
            Performs one public Backend GET. It does not generate a Case or run
            local scanner selection.
        """
        return validate_strategy_group_catalog(self.strategies())

    def judge_config(self) -> Mapping[str, Any]:
        """Fetch current public Judge upload limits and Evidence types.

        Returns:
            Backend configuration mapping used to bound official Evidence upload.

        Raises:
            KumaAuthenticationError: No usable key or rejected authentication.
            KumaPermissionError: Key cannot read Judge configuration.
            KumaRateLimitError: Account quota prevents the read.

        Side Effects:
            Performs one public Backend GET request.
        """

        return self._read("/sdk/judge/config/")


__all__ = ["DEFAULT_BASE_URL", "KumaClient", "Transport"]
