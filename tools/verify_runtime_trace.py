"""Verify real OTel capture and Official Judge serialization without networking."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from kuma.contracts import (
    CaptureStatus,
    Case,
    FileChange,
    FileEvidence,
    HistoryItem,
    KumaInput,
    Submission,
)
from kuma.errors import ProviderError
from kuma.evidence.runtime import build_runtime_evidence, runtime_submission_id
from kuma.evidence.runtime_contract import (
    RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA,
    runtime_evidence_json,
)
from kuma.otel import configure_trace_evidence
from kuma.providers._official_evidence_upload import (
    evidence_upload,
    judge_upload_config,
)
from kuma.providers.base import JudgeContext
from kuma.providers.official_judge import OfficialJudgeProvider


def capture_context(mode="complete", *, argument=2):
    """Execute a local addition under real OTel, then prepare one immutable step."""
    provider = TracerProvider(resource=Resource({}))
    capture = configure_trace_evidence(provider)
    capture.begin_step("run-example", "case-example", "step-example")
    with provider.get_tracer("local-example").start_as_current_span(
        "execute_tool add"
    ) as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", "add")
        if mode != "not_recorded":
            arguments = {"left": argument, "right": 3}
            if mode == "sensitive":
                arguments["api_key"] = "sk-" + "a" * 30
            span.set_attribute("gen_ai.tool.call.arguments", json.dumps(arguments))
            result = argument + 3
            span.set_attribute("gen_ai.tool.call.result", json.dumps(result))
    prepared = capture.prepare_step("run-example", "case-example", "step-example")
    trace = prepared.evidence
    built = build_runtime_evidence(
        run_id="run-example",
        input_id="step-example",
        step_id="step-example",
        submission_id=runtime_submission_id("run-example", "step-example"),
        root=Path.cwd(),
        status="completed",
        output="done",
        error=None,
        file_evidence=None,
        logs=(),
        trace_evidence=trace,
    )
    item = KumaInput(
        "run-example", "case-example", "step-example", 0, "text", "add locally"
    )
    submission = Submission(
        run_id=item.run_id,
        case_id=item.case_id,
        input_id=item.input_id,
        status="completed",
        output="done",
        capture_status=CaptureStatus(traces=prepared.component),
        extensions={
            "trace_evidence": trace,
            "trace_capture_summary": prepared.capture_summary,
            "runtime_evidence": built.evidence,
        },
    )
    prepared.commit()
    provider.shutdown()
    return JudgeContext(
        Case((item,), case_id=item.case_id),
        (HistoryItem(item, submission),),
        "completed",
    )


def upload_config(*, trace=True):
    """Return a synthetic public config; advertising file_diff does not select it."""
    result = {
        "allowed_extensions": [".json"],
        "max_files": 20,
        "max_file_bytes": 5 * 1024 * 1024,
        "max_total_bytes": 8 * 1024 * 1024,
        "manifest_schema_version": "1",
        "evidence_types": ["raw_log", "defuzex.runtime_evidence.v2"],
    }
    if trace:
        result["evidence_types"].append(RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA)
        result["runtime_evidence_capabilities"] = [
            "runtime_evidence",
            "agent_output",
            "file_diff",
            "runtime_trace",
        ]
    return result


class LocalTransport:
    """Record a single fake external operation; SDK serializers remain real."""

    base_url = "https://example.invalid/api/agentdefuze"

    def __init__(self, *, trace=True):
        self.trace = trace
        self.posts = []

    def json(self, method, path, payload=None, *, idempotency_key=None):
        if path == "/sdk/judge/config/":
            return upload_config(trace=self.trace)
        if method == "GET" and path == "/sdk/v2/operations/op-example/":
            return {
                "operation_id": "op-example",
                "status": "succeeded",
                "result": {
                    "judgment_id": "33333333-3333-4333-8333-333333333333",
                    "status": "pass",
                    "confidence": "high",
                    "issues": [],
                    "step_results": [],
                    "flags": {"forced": False},
                },
            }
        raise AssertionError("Unexpected synthetic transport call")

    def multipart(self, path, fields, parts, *, idempotency_key):
        self.posts.append((path, fields, parts))
        return {"operation_id": "op-example", "status": "queued", "poll_after_ms": 100}


class RuntimeTraceSmoke(unittest.TestCase):
    def test_explicit_catalog_without_file_diff_rejects_before_post(self):
        transport = LocalTransport()
        config = upload_config()
        config["runtime_evidence_capabilities"].remove("file_diff")
        context = replace(capture_context(), upload_diff=True)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(transport, "json", return_value=config),
            self.assertRaises(ProviderError) as caught,
        ):
            OfficialJudgeProvider(transport, state_root=Path(directory)).judge(context)
        self.assertEqual(caught.exception.code, "runtime_evidence_unsupported")
        self.assertEqual(transport.posts, [])

    def test_file_diff_and_trace_compose_without_whole_file_upload(self):
        context = capture_context()
        item = context.history[0]
        root = Path.cwd()
        files = FileEvidence(
            complete=True,
            scope="local",
            changes=(
                FileChange(
                    path=str(root / "note.txt"),
                    change_type="modified",
                    diff="--- note.txt\n+++ note.txt\n@@ -1 +1 @@\n-old\n+new\n",
                ),
            ),
        )
        built = build_runtime_evidence(
            run_id=item.submission.run_id,
            input_id=item.submission.input_id,
            step_id=item.test_input.input_id,
            submission_id=runtime_submission_id(
                item.submission.run_id, item.submission.input_id
            ),
            root=root,
            status="completed",
            output="done",
            error=None,
            file_evidence=files,
            logs=(),
            trace_evidence=item.submission.extensions["trace_evidence"],
        )
        submission = replace(
            item.submission,
            file_evidence=files,
            extensions={
                **item.submission.extensions,
                "runtime_evidence": built.evidence,
            },
        )
        wire = self.wire(
            replace(
                context,
                history=(replace(item, submission=submission),),
                upload_diff=True,
            )
        )
        self.assertEqual(
            wire["capabilities"],
            ["runtime_evidence", "agent_output", "file_diff", "runtime_trace"],
        )
        change = next(c for c in wire["components"] if c["kind"] == "file_change")
        self.assertEqual(
            change["diff"]["text"],
            "--- note.txt\n+++ note.txt\n@@ -1 +1 @@\n-old\n+new\n",
        )
        self.assertEqual(sum("trace_evidence" in c for c in wire["components"]), 1)

    def wire(self, context):
        parts, _, _ = evidence_upload(context, judge_upload_config(upload_config()), "")
        return json.loads(parts[0].data)

    def test_actual_body_reaches_official_multipart(self):
        transport = LocalTransport()
        with tempfile.TemporaryDirectory() as directory:
            result = OfficialJudgeProvider(transport, state_root=Path(directory)).judge(
                capture_context()
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(transport.posts), 1)
        part = next(
            p for p in transport.posts[0][2] if "runtime-evidence" in p.content_type
        )
        wire = json.loads(part.data)
        self.assertEqual(
            wire["capabilities"], ["runtime_evidence", "agent_output", "runtime_trace"]
        )
        artifact = wire["components"][0]
        span = artifact["trace_evidence"]["spans"][0]
        self.assertEqual(span["attributes"]["gen_ai.tool.call.result"], 5)
        self.assertEqual(span["status"], "unset")
        encoded = runtime_evidence_json(artifact["trace_evidence"]).encode()
        self.assertEqual(
            (artifact["sha256"], artifact["size_bytes"]),
            (hashlib.sha256(encoded).hexdigest(), len(encoded)),
        )

    def test_same_final_output_different_tool_body_is_preserved(self):
        first, second = (
            self.wire(capture_context(argument=2)),
            self.wire(capture_context(argument=9)),
        )
        self.assertEqual(first["components"][-1], second["components"][-1])
        for value, expected in ((first, 5), (second, 12)):
            self.assertEqual(
                value["components"][0]["trace_evidence"]["spans"][0]["attributes"][
                    "gen_ai.tool.call.result"
                ],
                expected,
            )

    def test_unsupported_server_fails_before_post(self):
        transport = LocalTransport(trace=False)
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(ProviderError) as caught,
        ):
            OfficialJudgeProvider(transport, state_root=Path(directory)).judge(
                capture_context()
            )
        self.assertEqual(caught.exception.code, "runtime_evidence_unsupported")
        self.assertEqual(transport.posts, [])

    def test_missing_and_sensitive_remain_partial(self):
        for mode, status, dropped in (
            ("not_recorded", "not_recorded", 0),
            ("sensitive", "sensitive_content", 1),
        ):
            artifact = self.wire(capture_context(mode))["components"][0]
            self.assertEqual(artifact["capture_status"], "partial")
            self.assertEqual(artifact["trace_evidence"]["dropped_count"], dropped)
            self.assertEqual(
                artifact["trace_evidence"]["spans"][0]["tool_content_status"][
                    "arguments"
                ],
                status,
            )
            self.assertNotIn("sk-" + "a" * 30, json.dumps(artifact))

    def test_no_trace_keeps_old_v2_wire(self):
        context = capture_context()
        history = context.history[0]
        extensions = {
            "runtime_evidence": history.submission.extensions["runtime_evidence"]
        }
        context = replace(
            context,
            history=(
                replace(
                    history,
                    submission=replace(history.submission, extensions=extensions),
                ),
            ),
        )
        parts, _, _ = evidence_upload(
            context, judge_upload_config(upload_config(trace=False)), ""
        )
        value = json.loads(parts[0].data)
        self.assertEqual(value["schema_version"], "defuzex.runtime_evidence.v2")
        self.assertNotIn("capabilities", value)

    def test_invalid_advertisement_stays_closed(self):
        for wrong in (
            None,
            [],
            ["runtime_trace"],
            ["runtime_evidence", "agent_output", "private"],
        ):
            value = upload_config()
            value["runtime_evidence_capabilities"] = wrong
            with self.assertRaises(ProviderError):
                judge_upload_config(value)


if __name__ == "__main__":
    unittest.main()
