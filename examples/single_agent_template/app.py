"""Framework-neutral Single Agent Template for the public KUMA SDK."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol, TypeAlias

from kuma import ConfigurationError, KumaError, create_run
from kuma.providers import CaseGenerationContext

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


class AgentAdapter(Protocol):
    """One replaceable boundary between this template and a user's Agent."""

    def __call__(self, test_input: Any, *, timeout_seconds: float) -> JsonValue: ...


class AgentResultError(RuntimeError):
    """Raised after an Agent failure is truthfully submitted to the Run."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# USER-OWNED: replace only this function with the real Agent invocation.
def call_your_agent(test_input: Any, *, timeout_seconds: float) -> JsonValue:
    """Return JSON data, enforce the deadline, and raise on Agent failure.

    A real adapter should configure its Agent framework or HTTP client with
    ``timeout_seconds``. Raise ``TimeoutError`` when that deadline expires and
    let all other Agent exceptions propagate. Do not return a success-shaped
    placeholder after a failure.
    """

    return {
        "message": "Deterministic fake Agent completed",
        "received_input": test_input,
        "timeout_seconds": timeout_seconds,
    }


def _local_case(_context: CaseGenerationContext) -> Mapping[str, Any]:
    return {
        "case_id": "case_single_agent_smoke",
        "input_type": "text",
        "inputs": [
            {
                "input_id": "input_single_agent_smoke",
                "payload_type": "text",
                "payload": "Return one deterministic Agent result.",
            }
        ],
    }


def _environment_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ConfigurationError(f"{name} must be 1/0, true/false, or yes/no")


def _agent_timeout() -> float:
    raw = os.environ.get("KUMA_AGENT_TIMEOUT_SECONDS", "30")
    try:
        value = float(raw)
    except ValueError:
        raise ConfigurationError(
            "KUMA_AGENT_TIMEOUT_SECONDS must be a positive number"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError("KUMA_AGENT_TIMEOUT_SECONDS must be a positive number")
    return value


@contextmanager
def _repository(use_official: bool):
    configured = os.environ.get("KUMA_REPO_PATH")
    if configured:
        yield Path(configured)
        return
    if use_official:
        raise ConfigurationError("Official mode requires KUMA_REPO_PATH")
    with TemporaryDirectory(prefix="kuma-single-agent-") as temporary:
        repo = Path(temporary)
        (repo / "README.md").write_text(
            "# Single Agent temporary smoke repository\n", encoding="utf-8"
        )
        yield repo


def _validated_result(result: JsonValue) -> JsonValue:
    if result is None or (isinstance(result, str) and not result.strip()):
        raise AgentResultError("agent_empty_result")
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise AgentResultError("agent_result_not_json") from None
    return result


def _submit_failure(run: Any, *, status: str, error: str) -> None:
    run.submit(status=status, error=error)


def _execute_step(
    run: Any,
    test_input: Any,
    *,
    agent: AgentAdapter,
    timeout_seconds: float,
) -> None:
    try:
        result = agent(test_input, timeout_seconds=timeout_seconds)
    except TimeoutError as exc:
        _submit_failure(run, status="timeout", error="Agent deadline expired")
        raise AgentResultError("agent_timeout") from exc
    except Exception as exc:
        _submit_failure(run, status="failed", error="Agent invocation failed")
        raise AgentResultError("agent_failed") from exc
    try:
        validated = _validated_result(result)
    except AgentResultError:
        _submit_failure(run, status="failed", error="Agent returned invalid output")
        raise
    run.submit(validated)


def _run_inputs(run: Any, *, agent: AgentAdapter, timeout_seconds: float) -> None:
    while True:
        test_input = run.get_input()
        if test_input is None:
            return
        _execute_step(
            run,
            test_input,
            agent=agent,
            timeout_seconds=timeout_seconds,
        )


def _print_result(run: Any) -> None:
    last_status = run.history[-1].submission.status if run.history else None
    print(f"run_state={run.state}")
    print(f"history_items={len(run.history)}")
    print(f"last_submission_status={last_status}")
    print(f"report={run.report!r}")
    # The current public TestReport contract has no canonical result URL.
    print("result_link=None")


def _smoke_agent(mode: str | None) -> AgentAdapter:
    if mode is None:
        return call_your_agent

    def failure(test_input: Any, *, timeout_seconds: float) -> JsonValue:
        del test_input, timeout_seconds
        if mode == "timeout":
            raise TimeoutError("deterministic smoke timeout")
        if mode == "empty":
            return ""
        if mode == "non-json":
            return {"invalid": {1}}  # type: ignore[return-value]
        raise RuntimeError("deterministic smoke Agent failure")

    return failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke-failure",
        choices=("agent", "timeout", "empty", "non-json"),
        help="Exercise a deterministic failure path without calling an Agent.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    use_official = _environment_flag("KUMA_USE_OFFICIAL", default=False)
    agent_profile = Path(
        os.environ.get(
            "KUMA_AGENT_PROFILE_PATH",
            Path(__file__).with_name("agent-profile.md"),
        )
    )
    with _repository(use_official) as repo:
        run = create_run(
            repo_path=repo,
            agent_profile_path=agent_profile,
            case_provider=None if use_official else _local_case,
            max_steps=None if use_official else 1,
            judge=use_official,
            on_failure="stop",
            allow_local=_environment_flag("KUMA_ALLOW_LOCAL", default=not use_official),
        )
        try:
            _run_inputs(
                run,
                agent=_smoke_agent(args.smoke_failure),
                timeout_seconds=_agent_timeout(),
            )
        except AgentResultError as exc:
            _print_result(run)
            print(f"template_error={exc.code}", file=sys.stderr)
            return 1
        _print_result(run)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KumaError as exc:
        print(
            f"sdk_error={exc.code} retryable={str(exc.retryable).lower()}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
