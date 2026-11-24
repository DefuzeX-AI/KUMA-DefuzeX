"""Consume experiment wrappers offline; no credentials, HTTP or model calls."""

from typing import Any


def summarize_experiment(result: dict[str, Any]) -> dict[str, Any]:
    """Read the outer wrapper without treating stopped reviews as completed reviews.

    Args:
        result: A normal run_experiment return with experiment, reviews and errors.
    Returns:
        Local counts, optional saved experiment ID and safe per-trace error categories.
    Side Effects:
        None. This does not retry work, infer Agent quality or persist report bodies.
    """
    return {
        "experiment_id": None
        if result["experiment"] is None
        else result["experiment"]["id"],
        "completed": sum(r["review_status"] == "completed" for r in result["reviews"]),
        "stopped": sum(r["review_status"] == "stopped" for r in result["reviews"]),
        "errors": result["errors"],
    }


def main() -> None:
    """Show three synthetic normal-return shapes, not purported hosted receipts."""
    # Display-only fixture IDs are intentionally not valid service document IDs.
    fixtures = {
        "all_completed": {
            "experiment": {"id": "example-experiment"},
            "reviews": [{"review_status": "completed"}, {"review_status": "completed"}],
            "errors": [],
        },
        "partial_failure": {
            "experiment": {"id": "example-experiment"},
            "reviews": [{"review_status": "stopped"}],
            "errors": [{"trace_id": "example-trace-2", "code": "service_busy"}],
        },
        "all_failed": {
            "experiment": None,
            "reviews": [],
            "errors": [{"trace_id": "example-trace-1", "code": "service_busy"}],
        },
    }
    for label, result in fixtures.items():
        print(label, summarize_experiment(result))


if __name__ == "__main__":
    main()
