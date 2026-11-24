"""Prepare a synthetic local review bundle without an API key or Agent execution."""

from __future__ import annotations

import argparse
from pathlib import Path

from kuma.trajectory import encode_review, prepare_review


def main() -> None:
    """Create one caller-selected file exclusively; never overwrite a prior input."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = prepare_review(
        "Inspect the object dimensions. Keep the model unchanged.",
        [
            {
                "event": "tool_result",
                "turn_id": "turn-1",
                "tool": "inspect_object",
                "ok": False,
                "error_code": "TOOL_EXECUTION_FAILED",
                "code_sha256": "before",
            },
            {
                "event": "tool_result",
                "turn_id": "turn-1",
                "tool": "inspect_object",
                "ok": True,
                "result": {"width": 8.0},
                "code_sha256": "after",
            },
        ],
        sample="synthetic-inspection-retry",
        tools=[
            {
                "name": "inspect_object",
                "description": "Read object dimensions.",
                "effects": "read_only",
                "input_schema": {"type": "object"},
            }
        ],
        limitations=[
            "Synthetic development example, not an independent accuracy test."
        ],
    )
    with args.output.open("xb") as stream:
        stream.write(encode_review(bundle))


if __name__ == "__main__":
    main()
