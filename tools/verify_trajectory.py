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
        trace_id = receipt("traces")["id"]
        data = b'{"source_time_ns":1789783050660927701}'
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
