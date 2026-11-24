"""Explicit optional OpenInference wiring; never replace or remove user instrumentation."""

from __future__ import annotations

import importlib
import threading
from typing import Any

from .errors import ConfigurationError

_LOCK = threading.RLock()
_OWNED: dict[str, tuple[Any, Any]] = {}
_SUPPORTED = {"existing", "openai", "langchain", "langgraph"}


def _prepare_owned_source(key: str, instrumentor: Any) -> None:
    """Attach or retry provenance only for an installed KUMA-owned LangChain hook.

    The caller caches ownership before this fallible boundary. Attachment is
    idempotent; failure propagates to the stable configuration error and never
    converts the owned hook to external success. Other integrations are untouched.
    """
    if key == "langchain":
        from .capture_source import attach_langchain_source

        attach_langchain_source(instrumentor)


def validate_framework(framework: str) -> None:
    """Reject unsupported integration names before storage or instrumentation side effects."""
    if not isinstance(framework, str) or framework not in _SUPPORTED:
        raise ConfigurationError(
            "Choose existing, openai, langchain or langgraph capture"
        )


def prepare_instrumentation(
    framework: str, provider: Any
) -> tuple[Any, dict[str, str]]:
    """Wire one selected optional integration at most once per process.

    Args:
        framework: existing preserves all application wiring; openai selects Chat
            Completions, langchain/langgraph share the OpenInference LangChain hook.
        provider: Caller provider to preserve, or None to reuse a compatible global
            provider. With no such provider, selected integration gets a private SDK
            provider with default sampling and no exporter; it is never made global.
    Returns:
        Provider for observe(), plus safe integration status. Existing external
        instrumentation is never rebound; its actual provider may require explicit
        existing mode. Zero resulting spans remain an observable capture failure.
    Raises:
        ConfigurationError: Missing optional dependencies, incompatible provider,
            a different provider requested for a KUMA-owned process hook, or wiring
            failure. Raw dependency errors and values are not echoed.
    Side Effects:
        Explicit selected modes install process-wide instrumentation once. Hooks
        remain installed for reuse; no uninstrument, provider shutdown, sampling
        override, global provider replacement, exporter, key or network operation.
    """
    validate_framework(framework)
    if framework == "existing":
        return provider, {"framework": framework, "status": "application_owned"}
    key = "langchain" if framework == "langgraph" else framework
    try:
        with _LOCK:
            module = importlib.import_module(f"openinference.instrumentation.{key}")
            instrumentor = getattr(
                module,
                "OpenAIInstrumentor" if key == "openai" else "LangChainInstrumentor",
            )()
            owned = _OWNED.get(key)
            if instrumentor.is_instrumented_by_opentelemetry and owned is not None:
                if provider is not None and provider is not owned[0]:
                    raise ValueError("different provider")
                _prepare_owned_source(key, instrumentor)
                return owned[0], {"framework": framework, "status": "kuma_reused"}
            from opentelemetry.trace import get_tracer_provider

            chosen = provider if provider is not None else get_tracer_provider()
            if instrumentor.is_instrumented_by_opentelemetry:
                return chosen, {
                    "framework": framework,
                    "status": "external_binding_unverified",
                }
            if not callable(getattr(chosen, "add_span_processor", None)):
                if provider is not None:
                    raise ValueError("incompatible provider")
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider

                chosen = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
            instrumentor.instrument(tracer_provider=chosen)
            if not instrumentor.is_instrumented_by_opentelemetry:
                raise ValueError("instrumentation not installed")
            # Own the installed hook even if source attachment fails. A retry
            # must repair/reject our hook, never relabel it as external success.
            _OWNED[key] = (chosen, instrumentor)
            _prepare_owned_source(key, instrumentor)
            return chosen, {"framework": framework, "status": "kuma_enabled"}
    except Exception:
        pass  # Translate outside the handler so native dependency errors are not chained.
    raise ConfigurationError(
        "Capture integration unavailable. Install its capture extra and use a compatible "
        "provider; reuse an existing hook/provider instead of rebinding it."
    )
