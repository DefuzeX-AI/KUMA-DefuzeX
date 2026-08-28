"""Deterministic first run that never inspects a user repository or network."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .api import create_run
from .contracts import TestReport
from .providers import CaseGenerationContext, JudgeContext

QUICKSTART_INPUT = "Reply with exactly: kuma-ready"
QUICKSTART_EXPECTED_OUTPUT = "kuma-ready"
QUICKSTART_INPUT_SHA256 = hashlib.sha256(QUICKSTART_INPUT.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LocalQuickstartResult:
    """Stable beginner-facing result from one isolated local demonstration."""

    passed: bool
    score: int
    reason: str
    input_text: str
    input_sha256: str
    artifact_path: Path


class _LocalCaseProvider:
    requirement_required = False

    def generate_case(self, _context: CaseGenerationContext) -> dict[str, Any]:
        return {
            "case_id": "case_local_quickstart_v1",
            "input_type": "text",
            "rubric": {
                "rule": "exact_text_match",
                "expected_output": QUICKSTART_EXPECTED_OUTPUT,
            },
            "inputs": [
                {
                    "input_id": "input_local_quickstart_v1",
                    "payload_type": "text",
                    "payload": QUICKSTART_INPUT,
                }
            ],
        }


def _local_judge(context: JudgeContext) -> dict[str, Any]:
    output = context.history[0].submission.output
    passed = output == QUICKSTART_EXPECTED_OUTPUT
    reason = (
        "Output exactly matched the published rule."
        if passed
        else "Output did not exactly match the published rule."
    )
    return {
        "report_id": "report_local_quickstart_v1",
        "status": "pass" if passed else "issue",
        "confidence": 1.0,
        "stop_reason": "deterministic_exact_match",
        "issues": (
            []
            if passed
            else [
                {
                    "code": "exact_match_failed",
                    "message": reason,
                }
            ]
        ),
        "score": 100 if passed else 0,
        "reason": reason,
    }


def _write_artifact(*, output: str, score: int, reason: str) -> Path:
    artifact_root = Path(tempfile.mkdtemp(prefix="kuma-local-result-")).resolve()
    artifact = artifact_root / "result.json"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "defuzex.local-quickstart.v1",
                "evaluator": "exact_text_match",
                "input": QUICKSTART_INPUT,
                "input_sha256": QUICKSTART_INPUT_SHA256,
                "output": output,
                "passed": score == 100,
                "score": score,
                "reason": reason,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return artifact


def run_local_quickstart(*, demonstrate_failure: bool = False) -> LocalQuickstartResult:
    """Run the credential-free exact-match demo in an SDK-owned directory.

    This entry point intentionally supplies ``allow_local=True`` only for its
    own temporary repository. It never resolves credentials, constructs an
    official Provider, or evaluates files from the caller's working directory.
    """

    agent_output = (
        "not-the-expected-output" if demonstrate_failure else QUICKSTART_EXPECTED_OUTPUT
    )
    with tempfile.TemporaryDirectory(prefix="kuma-local-work-") as temporary:
        isolated_repo = Path(temporary).resolve()
        run = create_run(
            repo_path=isolated_repo,
            case_provider=_LocalCaseProvider(),
            judge_provider=_local_judge,
            max_inputs=1,
            judge=True,
            on_failure="stop",
            allow_local=True,
            track_files=False,
            save_local=False,
        )
        run.get_input()
        report = cast(TestReport, run.submit(agent_output))

    score = int(report.extensions["score"])
    reason = str(report.extensions["reason"])
    artifact_path = _write_artifact(
        output=agent_output,
        score=score,
        reason=reason,
    )
    return LocalQuickstartResult(
        passed=report.status == "pass",
        score=score,
        reason=reason,
        input_text=QUICKSTART_INPUT,
        input_sha256=QUICKSTART_INPUT_SHA256,
        artifact_path=artifact_path,
    )


__all__ = [
    "QUICKSTART_EXPECTED_OUTPUT",
    "QUICKSTART_INPUT",
    "QUICKSTART_INPUT_SHA256",
    "LocalQuickstartResult",
    "run_local_quickstart",
]
