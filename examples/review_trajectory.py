"""Review an existing trace through hosted KUMA using only KUMA_API_KEY; never rerun the Agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from kuma import KumaClient
from kuma.errors import KumaError, ValidationError
from kuma.trajectory_capture import prepare_observation, prepare_submission
from kuma.trajectory_requirements import requirements_template, validate_requirements


def load_json(path: Path, maximum: int) -> dict:
    """Read one explicit bounded UTF-8 JSON object without changing its integer precision."""
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Input exceeds the documented byte limit")
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Input must be a JSON object")
    return value


def save_json(path: Path, value: dict) -> None:
    """Write only to a caller-selected new file; no overwrite or automatic directory creation."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def upload_trace(workspace, source: dict, profile: dict) -> dict:
    """Submit one recognized recorded format without opening references or running an Agent."""
    schema = source.get("schema_version")
    if schema == "ATIF-v1.8":
        return workspace.upload_atif(source, profile)
    if schema == "kuma.trajectory_review_bundle.v1":
        return workspace.upload(prepare_submission(source, profile))
    if schema == "kuma.observation.v1":
        return workspace.upload(prepare_observation(source, profile))
    raise ValidationError(
        "Unsupported trace schema; use ATIF, a KUMA bundle or observation"
    )


def main(argv: list[str] | None = None) -> int:
    """Explicitly upload and review one trace; identical captures use a stable paid request key.

    Args:
        argv: Optional CLI arguments; None reads normal command-line arguments.
    Returns:
        Zero for template/completed review, two for stopped review, one for safe local/API errors.
    Side Effects:
        Template mode is local-only. Review mode explicitly uploads and requests fixed-fee Judge
        using the existing Kuma credential. No hidden retries, Agent execution or provider key.
        Output is a new file; after unknown timeout, rerun the same input/key to recover safely.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-requirements-template", type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--idempotency-key")
    args = parser.parse_args(argv)
    try:
        if args.write_requirements_template:
            if any((args.trace, args.requirements, args.output, args.idempotency_key)):
                parser.error("Template mode cannot be combined with review arguments")
            save_json(args.write_requirements_template, requirements_template())
            print(
                "Requirements template saved. Fill the actual task and known tool rules."
            )
            return 0
        if not all((args.trace, args.requirements, args.output)):
            parser.error("Review requires --trace, --requirements and --output")
        if args.output.exists():
            raise ValueError("Choose a new output path")
        profile = validate_requirements(load_json(args.requirements, 262144))
        source = load_json(args.trace, 4194304)
        workspace = KumaClient().trajectory()
        capture = upload_trace(workspace, source, profile)
        key = (
            args.idempotency_key
            or "trajectory-" + hashlib.sha256(capture["id"].encode()).hexdigest()
        )
        review = workspace.evaluate(capture["id"], idempotency_key=key)
        save_json(args.output, review)
        if review["review_status"] == "stopped":
            print("Review stopped. Inspect coverage and reason in the saved report.")
            return 2
        print(
            "Review completed; report saved. Task success is a separate evidence question."
        )
        return 0
    except KumaError as exc:
        print(
            f"Review failed ({exc.code or 'request_failed'}). Reuse the same input/key.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError):
        print(
            "Check input JSON, filled requirements and an unused writable output path.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
