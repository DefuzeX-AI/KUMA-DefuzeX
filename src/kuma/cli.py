from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import DEFAULT_BASE_URL, KumaClient
from .exceptions import KumaError
from .local_quickstart import run_local_quickstart
from .repository.strategy_groups import (
    available_evidence_capabilities,
    load_strategy_group_catalog,
    resolve_strategy_group,
)
from .repository.tool_capability_io import (
    load_agent_capabilities,
    save_agent_capabilities,
    scan_agent_tool_manifest,
)


def _emit(data: Any) -> None:
    """Write one JSON value to standard output for CLI callers."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _emit_or_save(data: Any, output: str | None) -> None:
    """Print JSON or atomically save it to an explicitly selected local path."""
    if output is None:
        _emit(data)
        return
    destination = Path(output).expanduser().resolve()
    if not destination.parent.is_dir():
        raise KumaError("Output directory does not exist", code="strategy_scan_invalid")
    encoded = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise KumaError(
            "Strategy Group output could not be saved",
            code="strategy_scan_invalid",
        ) from None


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


def _capability_summary(document: Any, *, path: str | None = None) -> dict[str, Any]:
    """Return a safe CLI summary without printing tool schemas or local content."""
    summary: dict[str, Any] = {
        "provenance": document.provenance,
        "schema_version": document.schema_version,
        "tool_count": len(document.tools),
    }
    if path is not None:
        summary["path"] = path
    return summary


def cmd_tools_scan(args: argparse.Namespace) -> int:
    """Normalize one explicit local manifest and atomically save a review draft."""
    document = scan_agent_tool_manifest(args.manifest)
    destination = save_agent_capabilities(document, args.output)
    _emit(_capability_summary(document, path=str(destination)))
    return 0


def cmd_tools_validate(args: argparse.Namespace) -> int:
    """Validate one canonical manual/scanner capability file without submitting it."""
    document = load_agent_capabilities(args.path)
    _emit(_capability_summary(document))
    return 0


def cmd_strategies_list(args: argparse.Namespace) -> int:
    """Fetch, validate, and print or save the public Strategy Group catalog."""
    base_url = args.base_url or os.environ.get("KUMA_BASE_URL", DEFAULT_BASE_URL)
    catalog = KumaClient(
        base_url=base_url, timeout=args.timeout
    ).strategy_group_catalog()
    _emit_or_save(catalog.to_dict(), args.output)
    return 0


def cmd_strategies_suggest(args: argparse.Namespace) -> int:
    """Suggest one group offline from an explicit catalog and capability file."""
    catalog = load_strategy_group_catalog(args.catalog)
    capabilities = load_agent_capabilities(args.capabilities)
    available = available_evidence_capabilities(capabilities, ())
    resolved = resolve_strategy_group(
        catalog,
        explicit=None,
        scan=True,
        available_capabilities=available,
    )
    _emit_or_save(resolved.to_declaration(), args.output)
    return 0


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
    tools = subparsers.add_parser(
        "tools",
        help="create or validate a local Agent tool capability contract",
    )
    tool_commands = tools.add_subparsers(dest="tools_command", required=True)
    scan = tool_commands.add_parser(
        "scan",
        help="normalize one explicit JSON tool manifest without executing tools",
    )
    scan.add_argument("manifest", help="explicit local JSON manifest to read")
    scan.add_argument(
        "--output",
        required=True,
        help="local canonical JSON file to create atomically",
    )
    scan.set_defaults(func=cmd_tools_scan)
    validate = tool_commands.add_parser(
        "validate",
        help="validate a canonical capability file without checking tool existence",
    )
    validate.add_argument("path", help="canonical local capability JSON file")
    validate.set_defaults(func=cmd_tools_validate)
    strategies = subparsers.add_parser(
        "strategies",
        help="list public Strategy Groups or suggest one from local capabilities",
    )
    strategy_commands = strategies.add_subparsers(
        dest="strategies_command", required=True
    )
    list_command = strategy_commands.add_parser(
        "list", help="fetch and validate the public Strategy Group catalog"
    )
    list_command.add_argument("--base-url")
    list_command.add_argument("--timeout", type=float, default=30.0)
    list_command.add_argument("--output", help="optional JSON destination")
    list_command.set_defaults(func=cmd_strategies_list)
    suggest = strategy_commands.add_parser(
        "suggest", help="suggest a group locally without network or tool execution"
    )
    suggest.add_argument("--catalog", required=True, help="local catalog JSON")
    suggest.add_argument(
        "--capabilities", required=True, help="local Agent capability JSON"
    )
    suggest.add_argument("--output", help="optional requirement-ready JSON destination")
    suggest.set_defaults(func=cmd_strategies_suggest)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments, invoke the selected command, and return its exit code."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KumaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
