"""Real OTLP/HTTP boundary, step isolation and shared Java/Python evidence semantics."""

import gzip
import http.client
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from kuma.errors import ConfigurationError
from kuma.evidence.trace import TraceEvidenceCapture, TraceEvidenceLimits
from kuma.otlp import OtlpTraceReceiver


def batch():
    """Construct actual OTLP Protobuf with a model parent and a completed tool child."""
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    resource.resource.attributes.add(
        key="service.name"
    ).value.string_value = "java-agent"
    scope = resource.scope_spans.add()
    scope.scope.name = "example-java-agent"
    model = scope.spans.add(
        name="chat fixture",
        trace_id=b"t" * 16,
        span_id=b"m" * 8,
        kind=3,
        start_time_unix_nano=100,
        end_time_unix_nano=200,
    )
    for key, value in {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "fixture",
        "gen_ai.input.messages": '[{"role":"user","content":"Add 2 and 3"}]',
        "gen_ai.output.messages": '[{"role":"assistant","content":"5"}]',
    }.items():
        model.attributes.add(key=key).value.string_value = value
    model.status.code = 1
    tool = scope.spans.add(
        name="execute_tool add",
        trace_id=b"t" * 16,
        span_id=b"x" * 8,
        parent_span_id=b"m" * 8,
        kind=1,
        start_time_unix_nano=110,
        end_time_unix_nano=150,
    )
    for key, value in {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "add",
        "gen_ai.tool.call.arguments": '{"left":2,"right":3}',
        "gen_ai.tool.call.result": '{"sum":5}',
    }.items():
        tool.attributes.add(key=key).value.string_value = value
    tool.status.code = 1
    return request


def post(environment, body, extra=None, *, path=None):
    """Send real loopback HTTP and return status plus response bytes."""
    url = urlsplit(environment["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"])
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=4)
    headers = {
        "Authorization": environment["OTEL_EXPORTER_OTLP_TRACES_HEADERS"].split("=", 1)[
            1
        ],
        "Content-Type": "application/x-protobuf",
        **(extra or {}),
    }
    try:
        connection.request("POST", path or url.path, body=body, headers=headers)
        response = connection.getresponse()
        return (response.status, response.read())
    finally:
        connection.close()


def evidence(capture, input_id="step-1"):
    """Prepare real sanitized evidence through the normal capture transaction."""
    return capture.prepare_step("run", "case", input_id)


class OtlpReceiverTests(unittest.TestCase):
    def setUp(self):
        """Isolate local Run workspaces for each protocol regression."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp_path = Path(directory.name)

    def test_real_protobuf_retains_model_tool_parent_and_resource(self):
        with OtlpTraceReceiver() as receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                status, response = post(env, batch().SerializeToString())
            assert status == 200
            assert (
                ExportTraceServiceResponse.FromString(
                    response
                ).partial_success.rejected_spans
                == 0
            )
            prepared = evidence(receiver.capture)
            model, tool = prepared.evidence["spans"]
            assert (
                model["model_content"]["input"]["value"][0]["content"] == "Add 2 and 3"
            )
            assert tool["parent_span_id"] == model["span_id"]
            assert tool["attributes"]["gen_ai.tool.call.arguments"] == {
                "left": 2,
                "right": 3,
            }
            assert tool["attributes"]["gen_ai.tool.call.result"] == {"sum": 5}
            assert tool["resource"]["service.name"] == "java-agent"
            prepared.commit()

    def test_late_old_token_never_enters_the_next_input(self):
        with OtlpTraceReceiver() as receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as old:
                assert post(old, batch().SerializeToString())[0] == 200
            evidence(receiver.capture).commit()
            receiver.capture.begin_step("run", "case", "step-2")
            with receiver.step() as new:
                assert old != new
                assert post(old, batch().SerializeToString())[0] == 401
                assert post(new, batch().SerializeToString())[0] == 200
            prepared = evidence(receiver.capture, "step-2")
            assert len(prepared.evidence["spans"]) == 2
            assert prepared.evidence["input_id"] == "step-2"
            prepared.commit()

    def test_sanitization_precedes_retention_and_detects_duplicate_ids(self):
        request = batch()
        span = request.resource_spans[0].scope_spans[0].spans[1]
        secret = "sk-" + "z" * 40
        for attribute in span.attributes:
            if attribute.key == "gen_ai.tool.call.arguments":
                attribute.value.string_value = json.dumps(
                    {"api_key": secret, "left": 2}
                )
        with OtlpTraceReceiver() as receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                assert post(env, request.SerializeToString())[0] == 200
                assert post(env, request.SerializeToString())[0] == 200
            prepared = evidence(receiver.capture)
            assert secret not in json.dumps(prepared.evidence)
            assert "trace_span_duplicate" in prepared.evidence["reasons"]
            assert prepared.component.status == "partial"

    def test_rejected_requests_retain_no_external_content(self):
        for change, status in [
            ("path", 404),
            ("bearer", 401),
            ("type", 415),
            ("encoding", 415),
            ("protobuf", 400),
        ]:
            with (
                self.subTest(change=change, status=status),
                OtlpTraceReceiver() as receiver,
            ):
                receiver.capture.begin_step("run", "case", "step-1")
                with receiver.step() as env:
                    headers = {"Authorization": "wrong"} if change == "bearer" else {}
                    if change == "type":
                        headers["Content-Type"] = "application/json"
                    if change == "encoding":
                        headers["Content-Encoding"] = "br"
                    payload = (
                        b"\xff" if change == "protobuf" else batch().SerializeToString()
                    )
                    code, _ = post(
                        env,
                        payload,
                        headers,
                        path="/wrong" if change == "path" else None,
                    )
                    assert code == status
                assert evidence(receiver.capture).evidence["spans"] == []

    def test_gzip_decoded_limit_and_batch_limit(self):
        with OtlpTraceReceiver(max_request_bytes=1000) as receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                raw = batch().SerializeToString()
                assert (
                    post(env, gzip.compress(raw), {"Content-Encoding": "gzip"})[0]
                    == 200
                )
                assert (
                    post(env, gzip.compress(b"a" * 1001), {"Content-Encoding": "gzip"})[
                        0
                    ]
                    == 413
                )
                assert post(env, b"a" * 1001)[0] == 413
                assert post(env, b"invalid", {"Content-Encoding": "gzip"})[0] == 400

    def test_invalid_identity_is_partial_success_and_never_synthesizes_span(self):
        request = batch()
        request.resource_spans[0].scope_spans[0].spans[0].trace_id = b"short"
        request.resource_spans[0].scope_spans[0].spans[1].parent_span_id = b""
        with OtlpTraceReceiver() as receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                code, body = post(env, request.SerializeToString())
            assert code == 200
            assert (
                ExportTraceServiceResponse.FromString(
                    body
                ).partial_success.rejected_spans
                == 1
            )
            assert len(evidence(receiver.capture).evidence["spans"]) == 1

    def test_steps_require_active_capture_and_release_socket_after_exception(self):
        receiver = OtlpTraceReceiver()
        with self.assertRaises(ConfigurationError), receiver.step():
            pass
        with receiver:
            with self.assertRaises(ConfigurationError), receiver.step():
                pass
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                port = urlsplit(env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]).port
                with self.assertRaises(ConfigurationError), receiver.step():
                    pass
                with self.assertRaises(ConfigurationError):
                    receiver.__enter__()
                receiver.capture.cancel_step()
                assert post(env, batch().SerializeToString())[0] == 409
        with socket.socket() as connection:
            assert connection.connect_ex(("127.0.0.1", port)) != 0
        receiver.__exit__()

    def test_invalid_configuration(self):
        for kwargs in [
            {"port": True},
            {"port": -1},
            {"port": 65536},
            {"max_request_bytes": 0},
            {"max_request_bytes": True},
            {"capture": object()},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigurationError):
                OtlpTraceReceiver(**kwargs)

    def test_capture_limits_and_missing_extra(self):
        capture = TraceEvidenceCapture(TraceEvidenceLimits(max_spans=1))
        with OtlpTraceReceiver(capture) as receiver:
            capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                assert post(env, batch().SerializeToString())[0] == 200
            assert len(evidence(capture).evidence["spans"]) == 1
        with (
            patch.dict(
                "sys.modules",
                {"opentelemetry.proto.collector.trace.v1.trace_service_pb2": None},
            ),
            self.assertRaisesRegex(ConfigurationError, "otlp"),
        ):
            OtlpTraceReceiver()

    def test_invalid_span_fields_report_omission(self):
        for change in ["duplicate", "time", "kind", "status", "depth"]:
            with self.subTest(change=change):
                request = batch()
                span = request.resource_spans[0].scope_spans[0].spans[1]
                if change == "duplicate":
                    span.attributes.add().CopyFrom(span.attributes[0])
                elif change == "time":
                    span.end_time_unix_nano = 1
                elif change == "kind":
                    span.kind = 99
                elif change == "status":
                    span.status.code = 99
                else:
                    value = span.attributes.add(key="gen_ai.usage.input_tokens").value
                    for _ in range(18):
                        value = value.array_value.values.add()
                    value.int_value = 1
                with OtlpTraceReceiver() as receiver:
                    receiver.capture.begin_step("run", "case", "step-1")
                    with receiver.step() as env:
                        code, body = post(env, request.SerializeToString())
                    assert code == 200
                    assert (
                        ExportTraceServiceResponse.FromString(
                            body
                        ).partial_success.rejected_spans
                        == 1
                    )
                    prepared = evidence(receiver.capture)
                    assert prepared.component.status == "partial"
                    assert len(prepared.evidence["spans"]) == 1

    def test_otlp_nested_values_links_events_and_upstream_drops(self):
        request = batch()
        span = request.resource_spans[0].scope_spans[0].spans[0]
        span.attributes.add(key="gen_ai.usage.input_tokens").value.int_value = 12
        array = span.attributes.add(key="gen_ai.usage.output_tokens").value.array_value
        array.values.add().double_value = 2.5
        array.values.add().bool_value = True
        nested = span.attributes.add(key="gen_ai.tool.call.result").value.kvlist_value
        nested.values.add(key="sum").value.int_value = 5
        span.attributes.add(key="unknown").value.bytes_value = b"discarded"
        span.links.add(trace_id=b"u" * 16, span_id=b"v" * 8)
        span.events.add(name="observed", time_unix_nano=120).attributes.add(
            key="gen_ai.usage.input_tokens"
        ).value.int_value = 3
        span.dropped_events_count = 1
        with OtlpTraceReceiver() as receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                assert post(env, request.SerializeToString())[0] == 200
            prepared = evidence(receiver.capture)
            mapped = prepared.evidence["spans"][0]
            assert mapped["attributes"]["gen_ai.usage.input_tokens"] == 12
            assert mapped["attributes"]["gen_ai.usage.output_tokens"] == [2.5, True]
            assert len(mapped["links"]) == 1 and len(mapped["events"]) == 1
            assert prepared.component.status == "partial"

    def test_batch_span_limit_and_gzip_members_account_for_loss(self):
        request = batch()
        span = request.resource_spans[0].scope_spans[0].spans[0]
        scope = request.resource_spans[0].scope_spans[0]
        for _ in range(1000):
            scope.spans.add().CopyFrom(span)
        with OtlpTraceReceiver() as receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                assert post(env, request.SerializeToString())[0] == 413
                compressed = gzip.compress(batch().SerializeToString())
                assert (
                    post(env, compressed[:-1], {"Content-Encoding": "gzip"})[0] == 400
                )
                assert (
                    post(env, compressed + b"extra", {"Content-Encoding": "gzip"})[0]
                    == 400
                )
            assert evidence(receiver.capture).component.status != "complete"

    def test_ambiguous_http_framing_is_rejected(self):
        for headers in [
            [("Content-Length", "1"), ("Content-Length", "1")],
            [("Content-Length", "-1")],
            [("Transfer-Encoding", "chunked")],
        ]:
            with self.subTest(headers=headers), OtlpTraceReceiver() as receiver:
                receiver.capture.begin_step("run", "case", "step-1")
                with receiver.step() as env:
                    url = urlsplit(env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"])
                    connection = http.client.HTTPConnection(
                        url.hostname, url.port, timeout=4
                    )
                    connection.putrequest("POST", "/v1/traces")
                    connection.putheader(
                        "Authorization",
                        env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"].split("=", 1)[1],
                    )
                    for name, value in headers:
                        connection.putheader(name, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    assert response.status == 400
                    response.read()
                    connection.close()

    def test_receiver_startup_failure_and_stopping_with_an_active_lease(self):
        with (
            patch("threading.Thread.start", side_effect=RuntimeError("startup")),
            self.assertRaisesRegex(RuntimeError, "startup"),
        ):
            OtlpTraceReceiver().__enter__()
        receiver = OtlpTraceReceiver()
        with receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step():
                receiver._server.handle_error(None, None)
                receiver.__exit__()

    def test_truncated_body_and_nonascii_bearer_fail_without_leaking_payload(self):
        with OtlpTraceReceiver() as receiver:
            receiver.capture.begin_step("run", "case", "step-1")
            with receiver.step() as env:
                assert post(env, b"", {"Authorization": "Bearer " + chr(233)})[0] == 401
                url = urlsplit(env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"])
                bearer = env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"].split("=", 1)[1]
                with socket.create_connection(
                    (url.hostname, url.port), timeout=4
                ) as connection:
                    request = f"POST /v1/traces HTTP/1.0\r\nAuthorization: {bearer}\r\nContent-Type: application/x-protobuf\r\nContent-Length: 9\r\n\r\nx"
                    connection.sendall(request.encode("ascii"))
                    connection.shutdown(socket.SHUT_WR)
                    assert b"400" in connection.recv(4096)
            assert evidence(receiver.capture).component.status != "complete"

    def test_external_evidence_uses_the_existing_judge_wire(self):
        tmp_path = self.tmp_path
        from kuma import create_run
        from kuma.contracts import Case
        from kuma.evidence.runtime_contract import RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA
        from kuma.providers._official_evidence_upload import (
            evidence_upload,
            judge_upload_config,
        )
        from kuma.providers.base import JudgeContext

        profile = tmp_path / "agent.md"
        profile.write_text(
            "---\nagent_description: A Java addition Agent\ninput_type: text\n---\n\n## Production Use Scenario\nAdd numbers locally.\n\n## Behaviors to Test\nReturn the observed sum.\n\n## Known Limitations or Prohibited Behaviors\nUse only local fixtures.\n",
            encoding="utf-8",
        )

        def local_case(_context):
            return {
                "case_id": "java-wire",
                "input_type": "text",
                "inputs": [
                    {
                        "input_id": "step-1",
                        "payload_type": "text",
                        "payload": "Add 2 and 3",
                    }
                ],
            }

        with OtlpTraceReceiver() as receiver:
            run = create_run(
                repo_path=tmp_path,
                agent_profile_path=profile,
                case_provider=local_case,
                max_steps=1,
                judge=False,
                allow_local=True,
                track_files=False,
                trace_evidence=receiver.capture,
            )
            try:
                assert run.get_input() == "Add 2 and 3"
                with receiver.step() as env:
                    assert post(env, batch().SerializeToString())[0] == 200
                run.submit({"sum": 5})
            finally:
                if run.state in {"ready", "input_delivered"}:
                    run.cancel()
            context = JudgeContext(
                Case(tuple(h.test_input for h in run.history), case_id=run.case_id),
                run.history,
                run.state,
            )
            config = judge_upload_config(
                {
                    "allowed_extensions": [".json"],
                    "max_files": 20,
                    "max_file_bytes": 5 * 1024 * 1024,
                    "max_total_bytes": 8 * 1024 * 1024,
                    "manifest_schema_version": "1",
                    "evidence_types": ["raw_log", RUNTIME_EVIDENCE_CAPABILITIES_SCHEMA],
                    "runtime_evidence_capabilities": [
                        "runtime_evidence",
                        "agent_output",
                        "runtime_trace",
                    ],
                    "trace_content_schemas": ["kuma.model_content.v1"],
                }
            )
            parts, manifest, _ = evidence_upload(context, config, "")
            envelopes = [json.loads(part.data) for part in parts]
            trace = next(
                c["trace_evidence"]
                for e in envelopes
                for c in e["components"]
                if "trace_evidence" in c
            )
            assert len(trace["spans"]) == 2
            assert (
                trace["spans"][0]["model_content"]["output"]["value"][0]["content"]
                == "5"
            )
            assert trace["spans"][1]["attributes"]["gen_ai.tool.call.result"] == {
                "sum": 5
            }
            assert len(manifest["files"]) == len(parts)


if __name__ == "__main__":
    unittest.main()
