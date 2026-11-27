"""Installed-SDK capture commands; local operations are separate from explicit paid review."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from .capture import export_capture, judge_capture
from .capture_store import list_captures, output_target
from .client import DEFAULT_BASE_URL, KumaClient
from .errors import ValidationError
from .repository.case_artifact_io import _unique_object
from .trajectory_requirements import validate_requirements


def _rules(path: str | None) -> dict | None:
    """Read only an explicitly selected bounded optional advanced profile, before any HTTP."""
    if path is None:
        return None
    failed = False
    try:
        with open(path, "rb") as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError, RecursionError):
        failed = True
    if failed:
        raise ValidationError(
            "Advanced requirements must be bounded readable UTF-8 JSON"
        )
    return validate_requirements(data)


def cmd_capture_list(args: argparse.Namespace) -> int:
    """Print local metadata only; never upload or expose recorded task/tool bodies."""
    result = list_captures(
        repo_path=args.repo_path, storage_path=args.storage_path, limit=args.limit
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


def cmd_capture_export(args: argparse.Namespace) -> int:
    """Export one real durable capture atomically without overwriting an existing file."""
    export_capture(
        args.capture_id,
        args.output,
        repo_path=args.repo_path,
        storage_path=args.storage_path,
    )
    print(json.dumps({"capture_id": args.capture_id, "exported": True}))
    return 0


def cmd_capture_judge(args: argparse.Namespace) -> int:
    """Preflight local output/rules, explicitly Judge, retain result, then publish output.

    Auth uses only the ordinary Kuma credential. The stable default request identity
    survives process boundaries. A stopped report is written and exits2; it is not
    a successful task verdict. Any later publication failure retains the reviewed
    response in capture storage and can recover with identical capture/key.
    """
    requirements = _rules(args.requirements)
    with output_target(args.output) as publish:
        result = judge_capture(
            args.capture_id,
            repo_path=args.repo_path,
            storage_path=args.storage_path,
            client=KumaClient(
                base_url=args.base_url
                or os.environ.get("KUMA_BASE_URL", DEFAULT_BASE_URL),
                timeout=args.timeout,
            ),
            requirements=requirements,
            idempotency_key=args.idempotency_key,
            accept_media_omission=not args.reject_media,
        )
        publish(result)
    scope = result.get("coverage", {}).get("media_omission")
    print(
        json.dumps(
            {
                "capture_id": args.capture_id,
                "review_status": result["review_status"],
                "reasons": result["reason"],
                "review_scope": "text_and_structure" if scope else "supplied_source",
                "media_understanding": "not_performed" if scope else None,
            }
        )
    )
    return 0 if result["review_status"] == "completed" else 2


def add_capture_parser(subparsers: Any) -> None:
    """Register durable capture operations without importing optional framework libraries."""
    captures = subparsers.add_parser(
        "captures", help="list, export or explicitly Judge saved executions"
    )
    commands = captures.add_subparsers(dest="captures_command", required=True)
    listed = commands.add_parser(
        "list",
        help="list local capture IDs and gap metadata; saved does not mean spans were captured",
    )
    listed.add_argument("--limit", type=int, default=20)
    listed.set_defaults(func=cmd_capture_list)
    exported = commands.add_parser(
        "export", help="atomically export a saved capture; no upload or fee"
    )
    exported.add_argument("capture_id")
    exported.add_argument("--output", required=True)
    exported.set_defaults(func=cmd_capture_export)
    judged = commands.add_parser(
        "judge", help="explicit paid review with one Kuma credential"
    )
    judged.add_argument("capture_id")
    judged.add_argument("--output", required=True)
    judged.add_argument(
        "--reject-media",
        action="store_true",
        help="retain legacy media rejection and automatic key; default reviews retained text/structure",
    )
    judged.add_argument(
        "--idempotency-key",
        help="optional logical review identity; default is stable for this capture/input",
    )
    judged.add_argument(
        "--requirements", help="optional richer rules retaining the original task"
    )
    judged.add_argument(
        "--base-url", help="public Kuma Backend URL; never a model-provider URL"
    )
    judged.add_argument("--timeout", type=float, default=180)
    judged.set_defaults(func=cmd_capture_judge)
    for command in (listed, exported, judged):
        command.add_argument(
            "--repo-path", default=".", help="original Agent workspace"
        )
        command.add_argument(
            "--storage-path",
            help="original external storage parent; default ~/.kuma-captures",
        )
