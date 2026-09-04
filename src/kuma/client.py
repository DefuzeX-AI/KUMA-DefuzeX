"""Backward-compatible public client backed by the v4 HTTP boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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

Transport = WireTransport


class KumaClient:
    """Read account and public service configuration without creating a Run.

    Args:
        api_key: Optional opaque ``dfx_`` credential. ``None`` resolves
            ``KUMA_API_KEY`` and then the local credential file. Construction may
            remain unauthenticated, but read methods then raise an authentication
            error.
        base_url: Public Backend API base URL. Remote URLs require HTTPS;
            loopback HTTP is accepted for local integration testing.
        timeout: Positive finite timeout in seconds for each GET request.
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

        Security/Privacy:
            Stable public errors are exposed without returning raw remote bodies.
        """
        if self._backend is None:
            raise KumaAuthenticationError(
                401, "Set KUMA_API_KEY or pass api_key to KumaClient."
            )
        try:
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

    def strategies(self) -> Mapping[str, Any]:
        """Fetch the public active Case strategy catalog for explicit discovery.

        Returns:
            Backend-managed strategy mapping available to this credential.

        Raises:
            KumaAuthenticationError: No usable key or rejected authentication.
            KumaPermissionError: Key cannot read strategy configuration.
            KumaRateLimitError: Account quota prevents the read.

        Side Effects:
            Performs one public Backend GET. Case generation does not use this
            method as a client-side availability precheck.
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
        return validate_strategy_group_catalog(self._read("/sdk/strategies/"))

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
