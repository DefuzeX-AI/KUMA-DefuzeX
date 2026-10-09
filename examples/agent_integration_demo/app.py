"""Runnable KUMA Agent integration example with free mock mode and hosted mode."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from kuma import ConfigurationError, KumaError, create_run
from kuma.providers import CaseGenerationContext


def local_case(_context: CaseGenerationContext) -> dict[str, object]:
    """Provide a deterministic local Case without network access."""
    return {
        "case_id": "case_agent_integration_demo",
        "input_type": "text",
        "inputs": [
            {
                "input_id": "input_agent_demo",
                "payload_type": "text",
                "payload": "Return a short confirmation that the Agent received this input.",
            }
        ],
    }


def call_your_agent(test_input: Any) -> dict[str, object]:
    """Replace this function with the user's real Agent call."""
    return {
        "message": "Mock Agent completed successfully.",
        "received_input": test_input,
    }


def require_hosted_configuration() -> None:
    """Check required hosted configuration before creating a hosted Run."""
    missing: list[str] = []

    if not os.environ.get("KUMA_API_KEY", "").strip():
        missing.append("KUMA_API_KEY")

    if not os.environ.get("KUMA_REPO_PATH", "").strip():
        missing.append("KUMA_REPO_PATH")

    if missing:
        raise ConfigurationError("Hosted mode requires: " + ", ".join(missing))


def run_demo(hosted: bool) -> None:
    if hosted:
        require_hosted_configuration()
        repo = Path(os.environ["KUMA_REPO_PATH"])
        profile = Path(
            os.environ.get(
                "KUMA_AGENT_PROFILE_PATH",
                Path(__file__).with_name("agent-profile.md"),
            )
        )

        print("mode=hosted")
        print("warning=Hosted evaluation may consume credits.")

        run = create_run(
            repo_path=repo,
            agent_profile_path=profile,
            max_steps=1,
            judge=True,
            allow_local=False,
        )
    else:
        print("mode=mock")
        print("cost=free_local_no_api_call")

        with TemporaryDirectory(prefix="kuma-agent-demo-") as temporary:
            repo = Path(temporary)
            (repo / "README.md").write_text(
                "# Local KUMA Agent Integration Demo\n",
                encoding="utf-8",
            )
            (repo / "agent-profile.md").write_text(
                """---
agent_description: A deterministic local Agent integration demo
input_type: text
---

## Production Use Scenario
Demonstrate how a user connects an Agent to the KUMA Run protocol.

## Behaviors to Test
Return a short confirmation that the Agent received the provided input.

## Known Limitations or Prohibited Behaviors
Do not access the network or paths outside the temporary repository.
""",
                encoding="utf-8",
            )
            profile = repo / "agent-profile.md"

            run = create_run(
                repo_path=repo,
                agent_profile_path=profile,
                case_provider=local_case,
                max_steps=1,
                judge=False,
                allow_local=True,
            )

            _complete_run(run)
            return

    _complete_run(run)


def _complete_run(run: Any) -> None:
    test_input = run.get_input()

    if test_input is None:
        print("result=no_input")
        return

    result = call_your_agent(test_input)
    run.submit(result)

    print(f"run_state={run.state}")
    print(f"history_items={len(run.history)}")
    print(f"result={result}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hosted",
        action="store_true",
        help="Run against the hosted KUMA service. May consume credits.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run_demo(hosted=args.hosted)
    except ConfigurationError as exc:
        print(f"configuration_error={exc}", file=sys.stderr)
        return 2
    except KumaError as exc:
        print(
            f"sdk_error={exc.code} retryable={str(exc.retryable).lower()}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
