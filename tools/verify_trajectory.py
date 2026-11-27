"""Verify installed trajectory SDK over loopback HTTP; no account, provider or paid model calls."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from collections import deque
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ["KUMA_DISABLE_UPDATE_CHECK"] = "1"

from kuma import KumaClient
from kuma.errors import ProviderError, ValidationError
from kuma.trajectory_atif import prepare_atif
from kuma.trajectory_requirements import (
    requirements_fingerprint,
    requirements_template,
)

FIXTURES = Path(__file__).parent / "fixtures/trajectory"


def profile():
    """Declare only the synthetic task; unavailable tool metadata remains unknown."""
    value = requirements_template()
    value.update(
        name="Public trajectory protocol",
        version="1",
        objective="Inspect the object dimensions.",
    )
    value["success_criteria"] = [
        {
            "id": "inspect",
            "description": "Review the captured inspection.",
            "priority": "required",
            "evidence_expected": ["Recorded tool result"],
        }
    ]
    return value


def receipt(kind, payload=None):
    """Construct a synthetic, content-bound public receipt without private Core identities."""
    encoded = json.dumps(
        payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return {
        "id": "a" * 64 + "." + kind + "." + digest,
        "kind": kind,
        "content_sha256": digest,
        "summary": {"name": "Synthetic protocol fixture"},
    }


def review_payload(trace_id):
    """Load a synthetic public report envelope, not a live accuracy or billing benchmark."""
    value = json.loads((FIXTURES / "review.v4.json").read_text(encoding="utf-8"))
    value.update(
        trace_id=trace_id,
        review_status="completed",
        reason=None,
        attempts=value["report"]["review_model"]["attempts"],
        judge_version="b" * 64,
        requirements_sha256=requirements_fingerprint(profile()),
        case_id=None,
        agent_version=None,
    )
    return {**receipt("reviews", value), **value}


@contextmanager
def backend(replies, *, parent=False):
    """Run a disposable external HTTP boundary; retain only synthetic bodies and auth booleans."""
    responses, requests = deque(replies), []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            """Return one queued fixture while observing actual public serialization and headers."""
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(
                {
                    "path": self.path,
                    "data": data,
                    "authenticated": hmac.compare_digest(
                        self.headers.get("Authorization", ""),
                        "Bearer dfx_synthetic-trajectory-only",
                    ),
                    "key": self.headers.get("Idempotency-Key"),
                }
            )
            payload = json.dumps(
                {
                    "schema_version": "kuma.trajectory_workspace.v1",
                    "result": responses.popleft(),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            """Keep credentials and request contents out of the default HTTP logger."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = KumaClient(
            api_key="dfx_synthetic-trajectory-only",
            base_url=f"http://127.0.0.1:{server.server_port}/api/agentdefuze",
        )
        yield client if parent else client.trajectory(), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TrajectoryProtocolTests(unittest.TestCase):
    def source(self):
        return json.loads((FIXTURES / "atif.json").read_text(encoding="utf-8"))

    def test_local_atif_preserves_original_coordinates_and_large_integers(self):
        original = self.source()
        before = copy.deepcopy(original)
        bundle = prepare_atif(profile()["objective"], original)
        self.assertEqual(original, before)
        self.assertEqual(
            bundle["events"][0]["record"]["extra"]["source_time_ns"],
            1789783050660927701,
        )
        self.assertEqual(bundle["events"][5]["trajectory_id"], "child-a")
        self.assertTrue(all("source_pointer" in event for event in bundle["events"]))

    def test_atif_common_versions_preserve_optional_identity_states(self):
        for version in ("ATIF-v1.7", "ATIF-v1.8"):
            for explicit_null in (False, True):
                original = self.source()
                original["schema_version"] = version
                for key in ("trajectory_id", "session_id"):
                    if explicit_null:
                        original[key] = None
                    else:
                        del original[key]
                before = copy.deepcopy(original)
                bundle = prepare_atif(profile()["objective"], original)
                self.assertEqual(original, before)
                self.assertEqual(bundle["events"][0]["source_format"], version)
                self.assertTrue(
                    all(
                        event["trajectory_id_origin"] == "source_pointer_sha256"
                        for event in bundle["events"]
                    )
                )
                record = bundle["events"][0]["record"]
                self.assertEqual("session_id" in record, explicit_null)
                if explicit_null:
                    self.assertIsNone(record["session_id"])

    def test_atif_empty_session_metadata_remains_recorded(self):
        for version in ("ATIF-v1.7", "ATIF-v1.8"):
            for session in ("", "   ", "\t\n", None):
                original = self.source()
                original.update(schema_version=version, session_id=session)
                before = copy.deepcopy(original)
                bundle = prepare_atif(profile()["objective"], original)
                self.assertEqual(original, before)
                self.assertEqual(bundle["events"][0]["record"]["session_id"], session)
                self.assertNotIn("trajectory_id_origin", bundle["events"][0])

    def test_one_key_covers_upload_and_explicit_same_key_review_requests(self):
        capture = receipt("traces")
        reviewed = review_payload(capture["id"])
        with backend([capture, reviewed, reviewed]) as (client, requests):
            saved = client.upload_atif(self.source(), profile())
            first = client.evaluate(saved["id"], idempotency_key="synthetic-review-01")
            repeated = client.evaluate(
                saved["id"], idempotency_key="synthetic-review-01"
            )
        self.assertEqual(first, repeated)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request["authenticated"] for request in requests))
        self.assertTrue(requests[0]["path"].endswith("/sdk/trajectory/write/"))
        self.assertTrue(
            all(r["path"].endswith("/sdk/trajectory/review/") for r in requests[1:])
        )
        self.assertEqual([r["key"] for r in requests[1:]], ["synthetic-review-01"] * 2)
        self.assertTrue(first["coverage"]["source_delivery_complete"])
        self.assertEqual(
            first["report"]["segments"][0]["unconfirmed_model_output"]["status"],
            "unconfirmed",
        )

    def test_bad_keys_reject_before_any_transport(self):
        with backend([]) as (client, requests), self.assertRaises(ValidationError):
            client.evaluate(receipt("traces")["id"], idempotency_key="bad")
        self.assertEqual(requests, [])

    def test_raw_audit_cannot_claim_to_be_confirmed(self):
        trace_id = receipt("traces")["id"]
        value = review_payload(trace_id)
        value["report"]["segments"][0]["unconfirmed_model_output"]["status"] = (
            "confirmed"
        )
        with backend([value]) as (client, _requests), self.assertRaises(ProviderError):
            client.evaluate(trace_id, idempotency_key="synthetic-review-02")

    def test_export_preserves_original_integer_bytes(self):
        data = b'{"source_time_ns":1789783050660927701}'
        trace_id = "a" * 64 + ".traces." + hashlib.sha256(data).hexdigest()
        import gzip

        export = {
            "schema_version": "kuma.trajectory_export.v1",
            "filename": "traces.json",
            "document_id": trace_id,
            "media_type": "application/json",
            "encoding": "gzip+base64",
            "content": base64.b64encode(gzip.compress(data)).decode(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        with backend([export]) as (client, _requests):
            self.assertEqual(client.export("traces", trace_id), data)


class ExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "examples/review_trajectory.py"
        spec = importlib.util.spec_from_file_location("trajectory_example", path)
        assert spec is not None and spec.loader is not None
        cls.example = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.example)

    def arguments(self, requirements, output, *extra):
        return [
            "--trace",
            str(FIXTURES / "atif.json"),
            "--requirements",
            str(requirements),
            "--output",
            str(output),
            *extra,
        ]

    def test_local_output_and_override_errors_make_zero_http_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / "requirements.json"
            requirements.write_text(json.dumps(profile()), encoding="utf-8")
            occupied = root / "occupied.json"
            occupied.write_text("existing data", encoding="utf-8")
            blocked = root / "blocked.json"
            cases = [
                (root / "missing" / "report.json", ()),
                (occupied, ()),
                (root, ()),
                *[
                    (root / "report.json", ("--idempotency-key", key))
                    for key in ["bad", "", "invalid key", "x" * 201, "非ascii请求key"]
                ],
            ]
            with backend([], parent=True) as (parent, requests):
                for output, extra in cases:
                    with (
                        self.subTest(output=output.name, extra=extra),
                        patch.object(
                            self.example, "KumaClient", return_value=parent
                        ) as factory,
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(
                            self.example.main(
                                self.arguments(requirements, output, *extra)
                            ),
                            1,
                        )
                        factory.assert_not_called()
                original_open = Path.open

                def deny_target(path, *args, **kwargs):
                    if path == blocked:
                        raise PermissionError("Synthetic filesystem admission failure")
                    return original_open(path, *args, **kwargs)

                with (
                    patch.object(Path, "open", deny_target),
                    patch.object(
                        self.example, "KumaClient", return_value=parent
                    ) as factory,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(
                        self.example.main(self.arguments(requirements, blocked)), 1
                    )
                    factory.assert_not_called()
                self.assertEqual(requests, [])
            self.assertEqual(occupied.read_text(encoding="utf-8"), "existing data")
            self.assertFalse(blocked.exists())
            self.assertFalse((root / "report.json").exists())

    def test_failed_review_cleans_empty_reservation_and_same_path_can_recover(self):
        capture = receipt("traces")
        reviewed = review_payload(capture["id"])
        malformed = copy.deepcopy(reviewed)
        malformed["report"]["segments"][0]["unconfirmed_model_output"]["status"] = (
            "confirmed"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requirements = root / "requirements.json"
            requirements.write_text(json.dumps(profile()), encoding="utf-8")
            output = root / "report.json"
            with (
                backend([capture, malformed, capture, reviewed], parent=True) as (
                    parent,
                    requests,
                ),
                patch.object(self.example, "KumaClient", return_value=parent),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                args = self.arguments(
                    requirements, output, "--idempotency-key", "synthetic-recover-01"
                )
                self.assertEqual(self.example.main(args), 1)
                self.assertFalse(output.exists())
                self.assertEqual(self.example.main(args), 0)
                self.assertEqual(requests[1]["key"], requests[3]["key"])
                self.assertEqual(requests[1]["key"], "synthetic-recover-01")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), reviewed)

    def test_failure_retains_partial_or_replaced_output_and_handles_removed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            with (
                self.assertRaisesRegex(ValueError, "interrupted"),
                self.example.new_report(output) as stream,
            ):
                stream.write("partial result")
                raise ValueError("interrupted")
            self.assertEqual(output.read_text(encoding="utf-8"), "partial result")
            output.unlink()
            replacement = root / "replacement.json"
            replacement.write_text("", encoding="utf-8")
            with (
                self.assertRaisesRegex(ValueError, "interrupted"),
                self.example.new_report(output) as stream,
            ):
                stream.close()
                os.replace(replacement, output)
                raise ValueError("interrupted")
            self.assertTrue(output.exists())
            self.assertEqual(output.read_bytes(), b"")
            output.unlink()
            with (
                self.assertRaisesRegex(ValueError, "interrupted"),
                self.example.new_report(output) as stream,
            ):
                stream.close()
                output.unlink()
                raise ValueError("interrupted")
            self.assertFalse(output.exists())

    def test_template_and_invalid_requirements_never_construct_a_network_client(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "requirements.json"
            with (
                patch.object(self.example, "KumaClient") as factory,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    self.example.main(
                        ["--write-requirements-template", str(destination)]
                    ),
                    0,
                )
                self.assertEqual(
                    json.loads(destination.read_text(encoding="utf-8")),
                    requirements_template(),
                )
                with redirect_stderr(io.StringIO()):
                    result = self.example.main(
                        [
                            "--trace",
                            str(FIXTURES / "atif.json"),
                            "--requirements",
                            str(destination),
                            "--output",
                            str(Path(directory) / "review.json"),
                        ]
                    )
                self.assertEqual(result, 1)
                factory.assert_not_called()

    def test_runnable_example_uses_real_sdk_and_reuses_its_automatic_request_key(self):
        capture = receipt("traces")
        reviewed = review_payload(capture["id"])
        with tempfile.TemporaryDirectory() as directory:
            requirements = Path(directory) / "requirements.json"
            requirements.write_text(json.dumps(profile()), encoding="utf-8")
            with (
                backend([capture, reviewed, capture, reviewed], parent=True) as (
                    parent,
                    requests,
                ),
                patch.object(self.example, "KumaClient", return_value=parent),
                redirect_stdout(io.StringIO()),
            ):
                reports = []
                for index in range(2):
                    target = Path(directory) / f"review-{index}.json"
                    result = self.example.main(
                        [
                            "--trace",
                            str(FIXTURES / "atif.json"),
                            "--requirements",
                            str(requirements),
                            "--output",
                            str(target),
                        ]
                    )
                    self.assertEqual(result, 0)
                    reports.append(json.loads(target.read_text(encoding="utf-8")))
            self.assertEqual(reports[0], reports[1])
            self.assertEqual(requests[1]["key"], requests[3]["key"])
            self.assertTrue(all(request["authenticated"] for request in requests))


if __name__ == "__main__":
    unittest.main(verbosity=2)
