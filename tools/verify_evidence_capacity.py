"""Verify bounded Evidence capacity with synthetic, offline examples."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kuma.errors import LimitExceededError
from kuma.evidence.runtime import (
    build_runtime_evidence,
    project_runtime_evidence_v2,
    runtime_submission_id,
)
from kuma.evidence.runtime_contract import (
    RUNTIME_AGENT_OUTPUT_MAX_BYTES,
    RUNTIME_EVIDENCE_MAX_BYTES,
    RUNTIME_EVIDENCE_SCHEMA_V2,
    runtime_agent_output_bytes,
    runtime_claim_sha256,
    runtime_evidence_json,
    validate_runtime_evidence,
)
from kuma.evidence.trace import TraceEvidenceCapture, TraceEvidenceLimits
from kuma.evidence.trace_mapping import json_size
from kuma.transport.backend import BackendClient, UploadPart, encode_multipart

_MIB = 1024 * 1024
_MULTIPART_LIMIT = 8 * _MIB


def _span(step: int, index: int) -> SimpleNamespace:
    """Build one large but allowlisted ended span for deterministic capacity tests."""

    span_id = step * 1_000 + index + 1
    return SimpleNamespace(
        context=SimpleNamespace(trace_id=step + 1, span_id=span_id),
        parent=None,
        name="s" + "x" * 254,
        kind=SimpleNamespace(name="INTERNAL"),
        status=SimpleNamespace(status_code=SimpleNamespace(name="UNSET")),
        start_time=span_id * 10,
        end_time=span_id * 10 + 5,
        attributes={"gen_ai.request.model": "m" * 200},
        events=[],
        resource=SimpleNamespace(attributes={}),
        instrumentation_scope=SimpleNamespace(name="fixture", version="1"),
    )


class EvidenceCapacityTests(unittest.TestCase):
    def test_typed_envelope_accepts_five_mib_utf8_and_rejects_one_more(self) -> None:
        first = "x" * (RUNTIME_AGENT_OUTPUT_MAX_BYTES - 2)
        components = [
            {
                "component_id": f"claim-{i}",
                "sequence": i,
                "kind": "agent_response_claim",
                "claim_id": f"claim-{i}",
                "claim": "completed",
                "agent_output": output,
                "text_sha256": runtime_claim_sha256(output),
            }
            for i, output in enumerate((first, ""))
        ]
        envelope = {
            "schema_version": RUNTIME_EVIDENCE_SCHEMA_V2,
            "run_id": "run",
            "input_id": "step",
            "step_id": "step",
            "submission_id": "submission",
            "components": components,
        }
        remaining = RUNTIME_EVIDENCE_MAX_BYTES - len(
            runtime_evidence_json(envelope).encode("utf-8")
        )
        second = "é" * (remaining // 6) + "x" * (remaining % 6)
        components[1]["agent_output"] = second
        components[1]["text_sha256"] = runtime_claim_sha256(second)
        self.assertEqual(
            len(runtime_evidence_json(envelope).encode("utf-8")),
            RUNTIME_EVIDENCE_MAX_BYTES,
        )
        association = dict(
            run_id="run",
            input_id="step",
            step_id="step",
            submission_id="submission",
            schema_version=RUNTIME_EVIDENCE_SCHEMA_V2,
        )
        validate_runtime_evidence(envelope, **association)
        components[1]["agent_output"] += "x"
        components[1]["text_sha256"] = runtime_claim_sha256(second + "x")
        with self.assertRaisesRegex(ValueError, "byte limit"):
            validate_runtime_evidence(envelope, **association)

    def test_default_trace_budget_retains_spans_through_ten_large_steps(self) -> None:
        capture = TraceEvidenceCapture()
        encoded_sizes: list[int] = []

        for step in range(10):
            input_id = f"step-{step + 1}"
            capture.begin_step("run", "case", input_id)
            for index in range(200):
                capture.export_span(_span(step, index))
            prepared = capture.prepare_step("run", "case", input_id)
            self.assertIsNotNone(prepared.evidence)
            self.assertEqual(len(prepared.evidence["spans"]), 200)
            encoded_sizes.append(json_size(prepared.evidence))
            prepared.commit()

        self.assertEqual(TraceEvidenceLimits().max_total_bytes, 8 * _MIB)
        self.assertGreater(sum(encoded_sizes), 512_000)
        self.assertLessEqual(sum(encoded_sizes), 8 * _MIB)
        capture.finish_run("run", "case")

    def test_agent_output_accepts_four_mib_and_rejects_one_byte_more(self) -> None:
        exact = "x" * (RUNTIME_AGENT_OUTPUT_MAX_BYTES - 2)
        oversized = exact + "x"

        self.assertEqual(
            len(runtime_agent_output_bytes(exact)), RUNTIME_AGENT_OUTPUT_MAX_BYTES
        )
        self.assertEqual(
            len(runtime_agent_output_bytes(oversized)),
            RUNTIME_AGENT_OUTPUT_MAX_BYTES + 1,
        )
        self.assertEqual(RUNTIME_AGENT_OUTPUT_MAX_BYTES, 4 * _MIB)
        self.assertEqual(RUNTIME_EVIDENCE_MAX_BYTES, 5 * _MIB)
        submission_id = runtime_submission_id("run", "step")
        with tempfile.TemporaryDirectory() as directory:
            evidence = build_runtime_evidence(
                run_id="run",
                input_id="step",
                step_id="step",
                submission_id=submission_id,
                root=Path(directory),
                status="completed",
                output=exact,
                error=None,
                file_evidence=None,
                logs=(),
                trace_evidence=None,
            ).evidence
        projected = project_runtime_evidence_v2(
            evidence,
            run_id="run",
            input_id="step",
            step_id="step",
            submission_id=submission_id,
            status="completed",
            output=exact,
        )
        self.assertEqual(projected["components"][-1]["agent_output"], exact)
        with self.assertRaises(LimitExceededError) as raised:
            project_runtime_evidence_v2(
                evidence,
                run_id="run",
                input_id="step",
                step_id="step",
                submission_id=submission_id,
                status="completed",
                output=oversized,
            )
        self.assertEqual(raised.exception.code, "agent_output_too_large")

    def test_runtime_evidence_ceiling_fails_before_upload(self) -> None:
        output = {"answer": "safe"}
        submission_id = runtime_submission_id("run", "step")
        with tempfile.TemporaryDirectory() as directory:
            evidence = build_runtime_evidence(
                run_id="run",
                input_id="step",
                step_id="step",
                submission_id=submission_id,
                root=Path(directory),
                status="completed",
                output=output,
                error=None,
                file_evidence=None,
                logs=(),
                trace_evidence=None,
            ).evidence
        with (
            patch("kuma.evidence.runtime.RUNTIME_EVIDENCE_MAX_BYTES", 100),
            self.assertRaises(LimitExceededError) as raised,
        ):
            project_runtime_evidence_v2(
                evidence,
                run_id="run",
                input_id="step",
                step_id="step",
                submission_id=submission_id,
                status="completed",
                output=output,
            )
        self.assertEqual(raised.exception.code, "runtime_evidence_too_large")
        self.assertEqual(dict(raised.exception.details), {"max_utf8_bytes": 100})

    def test_multipart_total_accepts_eight_mib_and_rejects_one_byte_more(
        self,
    ) -> None:
        empty_part = UploadPart("logs", "run.json", "application/json", b"")
        _, empty_body = encode_multipart({}, (empty_part,))
        exact_data = b"x" * (_MULTIPART_LIMIT - len(empty_body))

        _, exact_body = encode_multipart(
            {},
            (UploadPart("logs", "run.json", "application/json", exact_data),),
        )
        self.assertEqual(len(exact_body), _MULTIPART_LIMIT)
        with self.assertRaises(LimitExceededError) as raised:
            encode_multipart(
                {},
                (
                    UploadPart(
                        "logs",
                        "run.json",
                        "application/json",
                        exact_data + b"x",
                    ),
                ),
            )
        self.assertEqual(raised.exception.code, "evidence_upload_too_large")

    def test_oversized_multipart_stops_before_transport(self) -> None:
        calls: list[object] = []

        def transport(*args: object) -> object:
            """Record an unexpected transport attempt."""

            calls.append(args)
            return {"status": "unexpected"}

        client = BackendClient(
            api_key="dfx_" + "a" * 40,
            transport=transport,  # type: ignore[arg-type]
        )
        with self.assertRaises(LimitExceededError):
            client.multipart(
                "/sdk/v2/judge/",
                {"manifest": json.dumps({"schema_version": "1", "files": []})},
                (
                    UploadPart(
                        "logs",
                        "run.json",
                        "application/json",
                        b"x" * _MULTIPART_LIMIT,
                    ),
                ),
                idempotency_key="idem-capacity",
            )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
