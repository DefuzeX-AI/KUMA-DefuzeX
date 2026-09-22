"""Run one deterministic KUMA Case offline; see minimal_local.md for adaptation."""

from pathlib import Path
from tempfile import TemporaryDirectory

from kuma import create_run
from kuma.providers import CaseGenerationContext


def local_case(_context: CaseGenerationContext) -> dict[str, object]:
    """Return one public text Input from a local custom Case Provider."""

    return {
        "case_id": "case_local_demo",
        "input_type": "text",
        "inputs": [
            {
                "input_id": "input_local_1",
                "payload_type": "text",
                "payload": "Return a bounded maintenance result.",
            }
        ],
    }


def call_your_agent(test_input: str) -> dict[str, str]:
    """Replace this deterministic body with your Agent's JSON-compatible result."""
    return {"message": "Completed locally", "input": test_input}


def main() -> int:
    with TemporaryDirectory(prefix="kuma-example-") as temporary:
        repo = Path(temporary)
        (repo / "README.md").write_text(
            "# Temporary example repository\n", encoding="utf-8"
        )
        agent_profile = repo / "agent-profile.md"
        agent_profile.write_text(
            """---
agent_description: A deterministic local example agent
input_type: text
---

## Production Use Scenario
Maintain a temporary example repository.

## Behaviors to Test
Return one bounded maintenance result.

## Known Limitations or Prohibited Behaviors
Do not access the network or paths outside the temporary repository.
""",
            encoding="utf-8",
        )

        run = create_run(
            repo_path=repo,
            agent_profile_path=agent_profile,
            case_provider=local_case,
            max_steps=1,
            judge=False,
            allow_local=True,
            track_files=False,
        )
        try:
            test_input = run.get_input()
            try:
                output = call_your_agent(test_input)
            except TimeoutError:
                run.submit(status="timeout", error="Agent timed out")
            except Exception:
                # Do not submit raw exception text, which can contain secrets.
                run.submit(status="failed", error="Agent failed")
            else:
                run.submit(output)

            submission_status = run.history[-1].submission.status
            print(f"input={test_input}")
            print(f"state={run.state}")
            print(f"submissions={len(run.history)}")
            print(f"submission_status={submission_status}")
            print(f"report={run.report}")
            return 0 if submission_status == "completed" else 1
        finally:
            # Release an unfinished Run on interruption or invalid Agent output.
            if run.state in {"ready", "input_delivered"}:
                run.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
