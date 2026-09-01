from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .client import DEFAULT_BASE_URL, KumaClient
from .exceptions import KumaError
from .local_quickstart import run_local_quickstart


def _emit(data: Any) -> None:
    """Write one JSON value to standard output for CLI callers."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_whoami(args: argparse.Namespace) -> int:
    """Fetch and print public entitlements for the configured API key."""
    base_url = args.base_url or os.environ.get("KUMA_BASE_URL", DEFAULT_BASE_URL)
    client = KumaClient(base_url=base_url, timeout=args.timeout)
    _emit(client.entitlements())
    return 0


def cmd_quickstart(args: argparse.Namespace) -> int:
    """Run the deterministic credential-free local quickstart command."""
    try:
        result = run_local_quickstart(demonstrate_failure=args.fail_demo)
    except Exception:
        print(
            "Local check could not finish. No account or network was used.",
            file=sys.stderr,
        )
        return 2
    outcome = "PASS" if result.passed else "FAIL"
    print(f"Local check: {outcome}")
    print(f"Score: {result.score}/100")
    print(f"Reason: {result.reason}")
    print(f"Artifact: {result.artifact_path}")
    return 0 if result.passed else 1


def build_parser() -> argparse.ArgumentParser:
    """Build the public CLI parser without performing I/O."""
    parser = argparse.ArgumentParser(prog="kuma")
    subparsers = parser.add_subparsers(dest="command", required=True)
    whoami = subparsers.add_parser(
        "whoami", help="validate KUMA_API_KEY and show entitlements"
    )
    whoami.add_argument("--base-url")
    whoami.add_argument("--timeout", type=float, default=30.0)
    whoami.set_defaults(func=cmd_whoami)
    quickstart = subparsers.add_parser(
        "quickstart",
        help="run a credential-free deterministic local first check",
    )
    quickstart.add_argument(
        "--fail-demo",
        action="store_true",
        help="show the deterministic failed-score output",
    )
    quickstart.set_defaults(func=cmd_quickstart)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments, invoke the selected command, and return its exit code."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KumaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
