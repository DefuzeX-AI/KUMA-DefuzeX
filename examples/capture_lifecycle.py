"""Run real instrumented libraries against offline model boundaries, then persist automatically.

Install examples/requirements-instrumentation.txt and KUMA. Select one framework.
No model key, outbound socket, cloud upload or paid Judge is used by this example.
The resulting ID works in a separate `kuma captures export` / `judge` process.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from kuma import capture


def run_sample(framework: str, repo_path: str, storage_path: str) -> dict:
    """Capture actual model/tool callbacks without constructing synthetic OTel spans."""
    path = Path(__file__).with_name("instrumented_observation.py")
    spec = importlib.util.spec_from_file_location("offline_capture_calls", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = {
        "openai": module.openai_call,
        "langchain": module.langchain_call,
        "langgraph": module.langgraph_call,
    }
    with (
        patch(
            "socket.socket.connect",
            side_effect=AssertionError("Model network forbidden"),
        ),
        capture(
            "Add 2 and 3.",
            framework=framework,
            repo_path=repo_path,
            storage_path=storage_path,
        ) as recorded,
    ):
        result = calls[framework]()
    document = recorded.export()
    return {
        "capture_id": recorded.capture_id,
        "result": result,
        "persistence_status": recorded.persistence_status,
        "capture_status": document["observation"]["capture_status"],
        "reasons": document["observation"]["reasons"],
    }


def main() -> None:
    """Print the durable ID; ordinary capture does not read a Kuma credential or call Judge."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--framework", required=True, choices=("openai", "langchain", "langgraph")
    )
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--storage-path", required=True)
    args = parser.parse_args()
    print(json.dumps(run_sample(args.framework, args.repo_path, args.storage_path)))


if __name__ == "__main__":
    main()
