"""Capture completed public messages locally; no official evaluation or network."""

from tempfile import TemporaryDirectory

from kuma import collect_public_messages, create_run


class LocalCase:
    """Provide one local step; agent_profile_required is False for this fixture."""

    agent_profile_required = False

    def generate_case(self, context):
        """Return the public Input without reading the repository or networking."""
        return {"inputs": ["Describe the completed local check."]}


def main():
    """Create a disposable local Run, retain two completed replies, and show coverage."""
    events = [
        {
            "schemaVersion": 1,
            "sequence": 1,
            "type": "item.completed",
            "item": {
                "id": "reply-1",
                "type": "agent_message",
                "content": "The probe failed; no compatibility conclusion is available.",
            },
        },
        {
            "schemaVersion": 1,
            "sequence": 2,
            "type": "item.completed",
            "item": {
                "id": "reply-2",
                "type": "agent_message",
                "content": "The local inspection is complete.",
            },
        },
    ]
    with TemporaryDirectory(prefix="kuma-messages-") as repository:
        run = create_run(
            repo_path=repository,
            case_provider=LocalCase(),
            max_steps=1,
            judge=False,
            track_files=False,
            allow_local=True,
        )
        run.get_input()
        run.submit(
            "The local inspection is complete.",
            public_messages=collect_public_messages(
                events,
                source_actor="target_agent",
                source_complete=True,
            ),
        )
        evidence = run.history[0].submission.extensions["assessment_evidence"]
        print(f"completed_public_messages={len(evidence['public_messages'])}")
        print(f"message_coverage={evidence['coverage']['public_messages']}")
        print("official_assessment=not_requested")


if __name__ == "__main__":
    main()
