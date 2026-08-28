"""Run one deterministic KUMA Case without credentials or network access."""

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


def main() -> None:
    with TemporaryDirectory(prefix="kuma-example-") as temporary:
        repo = Path(temporary)
        (repo / "README.md").write_text(
            "# Temporary example repository\n", encoding="utf-8"
        )
        requirement = repo / "requirement.md"
        requirement.write_text(
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
            requirement_path=requirement,
            case_provider=local_case,
            max_inputs=1,
            judge=False,
            allow_local=True,
            track_files=False,
        )
        test_input = run.get_input()
        run.submit({"message": "Completed locally", "input": test_input})

        print(f"input={test_input}")
        print(f"state={run.state}")
        print(f"submissions={len(run.history)}")


if __name__ == "__main__":
    main()
