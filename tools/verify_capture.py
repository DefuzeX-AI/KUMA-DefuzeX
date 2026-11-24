"""Installed capture/CLI conformance; optional real framework calls, synthetic external boundaries."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ["KUMA_DISABLE_UPDATE_CHECK"] = "1"

from kuma import capture, export_capture, judge_capture, list_captures
from kuma.capture_store import load_capture
from kuma.errors import ConfigurationError, ValidationError
from kuma.trajectory_requirements import requirements_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def module_file(name, path):
    """Load explicit source-side test/example assets; kuma itself must come from installation."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli(*arguments):
    """Execute the installed module in a separate process with a synthetic Kuma credential."""
    environment = dict(
        os.environ,
        KUMA_API_KEY="dfx_synthetic-trajectory-only",
        KUMA_DISABLE_UPDATE_CHECK="1",
    )
    return subprocess.run(
        [sys.executable, "-m", "kuma", "captures", *map(str, arguments)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


@contextmanager
def workspace_tree():
    """Create sibling workspace/artifact directories without accessing user files."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo, store = root / "workspace", root / "storage"
        repo.mkdir()
        store.mkdir()
        yield root, repo, store


@contextmanager
def judge_boundary(requirements, *, uncertain=False, stopped=False):
    """Run actual HTTP with recorded synthetic v4 payload; no semantic scoring claim.

    One simulated Judge callback is cached by logical request identity, including
    a first503 uncertain outcome. This checks client recovery identity, not real
    provider billing, whose service contract was accepted separately.
    """
    protocol = module_file(
        "capture_protocol_fixture", ROOT / "tools/verify_trajectory.py"
    )
    trace = protocol.receipt("traces")
    report = protocol.review_payload(trace["id"])
    report["requirements_sha256"] = requirements_fingerprint(requirements)
    if stopped:
        # The accepted stopped envelope retains coverage, with no completed report.
        report.update(review_status="stopped", reason="budget_exhausted", report=None)
    state = {"requests": [], "keys": set(), "callbacks": 0, "uncertain": uncertain}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            """Observe serialized public input and answer only workspace commands."""
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status = 200
            with lock:
                state["requests"].append(
                    {
                        "body": data,
                        "authenticated": self.headers.get("Authorization")
                        == "Bearer dfx_synthetic-trajectory-only",
                        "key": self.headers.get("Idempotency-Key"),
                    }
                )
                if data["action"] == "traces.import":
                    result = trace
                elif data["action"] == "reviews.run":
                    key = self.headers["Idempotency-Key"]
                    if key not in state["keys"]:
                        state["keys"].add(key)
                        state["callbacks"] += 1
                    result = copy.deepcopy(report)
                    if state["uncertain"]:
                        state["uncertain"], status = False, 503
                else:
                    result, status = {}, 400
            payload = json.dumps(
                {"schema_version": "kuma.trajectory_workspace.v1", "result": result}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            """Suppress the standard logger; never print credentials or request bodies."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/agentdefuze", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class DurableCaptureTests(unittest.TestCase):
    def test_optional_dependencies_absent_remain_local_and_honest(self):
        script = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'openai','openinference','langchain_core','langgraph','opentelemetry'}:
            raise ModuleNotFoundError('Optional package intentionally absent')
sys.meta_path.insert(0, Block())
from kuma import capture
from kuma.errors import ConfigurationError
try:
    with capture('Inspect.', framework='openai', repo_path=sys.argv[1], storage_path=sys.argv[2]):
        raise AssertionError('unavailable integration entered')
except ConfigurationError:
    pass
with capture('Inspect.', repo_path=sys.argv[1], storage_path=sys.argv[2]) as session:
    pass
assert session.export()['observation']['capture_status']=='failed'
assert session.export()['observation']['spans']==[]
assert session.persistence_status=='saved'
"""
        with workspace_tree() as (_, repo, store):
            result = subprocess.run(
                [sys.executable, "-c", script, str(repo), str(store)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_zero_capture_is_saved_exportable_and_not_judged(self):
        with workspace_tree() as (root, repo, store):
            with capture(
                "Inspect the object.",
                tracer_provider=object(),
                repo_path=repo,
                storage_path=store,
            ) as recorded:
                pass
            self.assertEqual(recorded.persistence_status, "saved")
            document = recorded.export()
            self.assertEqual(document["observation"]["capture_status"], "failed")
            self.assertEqual(document["observation"]["spans"], [])
            self.assertIsNone(document["requirements"]["tools"])
            self.assertFalse((repo / ".kuma").exists())
            result = cli(
                "export",
                recorded.capture_id,
                "--repo-path",
                repo,
                "--storage-path",
                store,
                "--output",
                root / "export.json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / "export.json").read_text()), document)
            rows = list_captures(repo_path=repo, storage_path=store)
            self.assertEqual(rows[0]["operation_counts"], {})
            with (
                self.assertRaises(ValidationError),
                patch("kuma.capture.KumaClient") as factory,
            ):
                judge_capture(recorded.capture_id, repo_path=repo, storage_path=store)
            factory.assert_not_called()

    def test_original_task_redaction_and_exclusive_output(self):
        with workspace_tree() as (root, repo, store):
            secret = "sk-" + "a" * 48
            with capture(
                "Inspect configuration api_key=" + secret,
                tracer_provider=object(),
                repo_path=repo,
                storage_path=store,
            ) as recorded:
                pass
            self.assertNotIn(secret, json.dumps(recorded.export()))
            self.assertIn(
                "redacted", " ".join(recorded.export()["requirements"]["limitations"])
            )
            output = root / "export.json"
            output.write_text("user data")
            with self.assertRaises(ConfigurationError):
                export_capture(
                    recorded.capture_id, output, repo_path=repo, storage_path=store
                )
            self.assertEqual(output.read_text(), "user data")
            with self.assertRaises(ConfigurationError):
                export_capture(
                    recorded.capture_id,
                    root / "missing" / "out.json",
                    repo_path=repo,
                    storage_path=store,
                )

    def test_original_agent_failure_and_save_failure_remain_distinct(self):
        with workspace_tree() as (_, repo, store):
            failure = RuntimeError("original Agent failure")
            recorded = capture(
                "Inspect the object.",
                tracer_provider=object(),
                repo_path=repo,
                storage_path=store,
            )
            with (
                self.assertRaises(RuntimeError) as raised,
                patch(
                    "kuma.capture.save_capture",
                    side_effect=OSError("private OS detail"),
                ),
                recorded,
            ):
                raise failure
            self.assertIs(raised.exception, failure)
            self.assertEqual(recorded.persistence_error, "capture_save_failed")
            self.assertEqual(
                recorded.export()["observation"]["execution_status"], "failed"
            )
            with (
                self.assertRaises(ConfigurationError) as error,
                patch(
                    "kuma.capture.save_capture",
                    side_effect=OSError("private OS detail"),
                ),
                capture(
                    "Inspect.",
                    tracer_provider=object(),
                    repo_path=repo,
                    storage_path=store,
                ),
            ):
                pass
            self.assertNotIn("private OS detail", str(error.exception))
            self.assertIsNone(error.exception.__context__)


@unittest.skipUnless(
    os.environ.get("KUMA_RUN_INSTRUMENTATION_TESTS") == "1",
    "Install and opt in to the exact optional framework matrix",
)
class RealIntegrationTests(unittest.TestCase):
    def test_existing_provider_sampler_exporter_and_instrumentor_are_preserved(self):
        script = """
import importlib.util, sys
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ALWAYS_OFF
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_tracer_provider
from openinference.instrumentation.langchain import LangChainInstrumentor
from kuma import capture
spec=importlib.util.spec_from_file_location('calls',sys.argv[1]); calls=importlib.util.module_from_spec(spec);spec.loader.exec_module(calls)
for sampler in [ALWAYS_ON,ALWAYS_OFF]:
    provider=TracerProvider(sampler=sampler,resource=Resource({}),shutdown_on_exit=False)
    exporter=InMemorySpanExporter();provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor=LangChainInstrumentor();instrumentor.instrument(tracer_provider=provider)
    global_before=get_tracer_provider()
    with capture('Add 2 and 3.',framework='langchain',tracer_provider=provider,repo_path=sys.argv[2],storage_path=sys.argv[3]) as session:
        assert calls.langchain_call()=='5'
    expected=3 if sampler is ALWAYS_ON else 0
    assert len(session.export()['observation']['spans'])==expected
    assert len(exporter.get_finished_spans())==expected
    assert provider.sampler is sampler and get_tracer_provider() is global_before
    assert instrumentor.is_instrumented_by_opentelemetry
    assert session.export()['instrumentation']['status']=='external_binding_unverified'
    assert calls.langchain_call()=='5'
    assert len(exporter.get_finished_spans())==expected*2
    instrumentor.uninstrument();provider.shutdown()
"""
        with workspace_tree() as (_, repo, store):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(ROOT / "examples/instrumented_observation.py"),
                    str(repo),
                    str(store),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    @classmethod
    def setUpClass(cls):
        cls.demo = module_file(
            "real_capture_demo", ROOT / "examples/capture_lifecycle.py"
        )

    def run_demo(self, framework, repo, store):
        value = self.demo.run_sample(framework, str(repo), str(store))
        self.assertEqual(value["result"], "5")
        document, _ = load_capture(
            value["capture_id"], repo_path=repo, storage_path=store
        )
        return value["capture_id"], document

    def test_actual_three_frameworks_preserve_model_tool_and_parent_data(self):
        for framework, count in [("openai", 1), ("langchain", 3), ("langgraph", 5)]:
            with (
                self.subTest(framework=framework),
                workspace_tree() as (_, repo, store),
            ):
                _, document = self.run_demo(framework, repo, store)
                observation = document["observation"]
                spans = observation["spans"]
                self.assertEqual(len(spans), count)
                self.assertEqual(document["requirements"]["objective"], "Add 2 and 3.")
                self.assertEqual(observation["execution_status"], "completed")
                self.assertEqual(observation["capture_status"], "partial")
                self.assertIn("trace_attribute_not_allowlisted", observation["reasons"])
                identities = {(span["trace_id"], span["span_id"]) for span in spans}
                for span in spans:
                    if span["parent_span_id"]:
                        self.assertIn(
                            (span["trace_id"], span["parent_span_id"]), identities
                        )
                model = next(span for span in spans if "model_content" in span)
                self.assertIn(
                    "5", json.dumps(model["model_content"]["output"]["value"])
                )
                if framework != "openai":
                    tool = next(
                        span
                        for span in spans
                        if span["attributes"].get("gen_ai.operation.name")
                        == "execute_tool"
                    )
                    self.assertEqual(
                        tool["attributes"]["gen_ai.tool.call.arguments"],
                        {"a": 2, "b": 3},
                    )
                    self.assertEqual(tool["attributes"]["gen_ai.tool.call.result"], 5)

    def test_separate_process_export_judge_uncertain_outcome_and_same_key_recovery(
        self,
    ):
        with workspace_tree() as (root, repo, store):
            identifier, document = self.run_demo("langgraph", repo, store)
            exported = cli(
                "export",
                identifier,
                "--repo-path",
                repo,
                "--storage-path",
                store,
                "--output",
                root / "capture.json",
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual(json.loads((root / "capture.json").read_text()), document)
            with judge_boundary(document["requirements"], uncertain=True) as (
                url,
                state,
            ):
                args = [
                    "judge",
                    identifier,
                    "--repo-path",
                    repo,
                    "--storage-path",
                    store,
                    "--base-url",
                    url,
                ]
                failed = cli(*args, "--output", root / "review.json")
                self.assertEqual(failed.returncode, 1, failed.stderr)
                self.assertFalse((root / "review.json").exists())
                recovered = cli(*args, "--output", root / "review.json")
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                repeated = cli(*args, "--output", root / "replay.json")
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                self.assertEqual(state["callbacks"], 1)
                requests = state["requests"]
                keys = {
                    item["key"]
                    for item in requests
                    if item["body"]["action"] == "reviews.run"
                }
                self.assertEqual(len(keys), 1)
                self.assertTrue(all(item["authenticated"] for item in requests))
                submitted = requests[0]["body"]["data"]["submission"]
                self.assertEqual(submitted["requirements"]["objective"], "Add 2 and 3.")
                tool = next(
                    event["span"]
                    for event in submitted["bundle"]["events"]
                    if event.get("span", {})
                    .get("attributes", {})
                    .get("gen_ai.operation.name")
                    == "execute_tool"
                )
                self.assertEqual(tool["attributes"]["gen_ai.tool.call.result"], 5)
                before = len(requests)
                for extra, output in [
                    (["--idempotency-key", "bad"], root / "bad.json"),
                    ([], root / "missing" / "report.json"),
                    ([], root / "review.json"),
                ]:
                    result = cli(*args, *extra, "--output", output)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(len(requests), before)
            self.assertEqual(
                (root / "review.json").read_bytes(), (root / "replay.json").read_bytes()
            )
            self.assertTrue(list((store / ".kuma/captures").glob("review_*.json")))

    def test_concurrent_normal_tools_are_isolated_without_reinstrumenting(self):
        with workspace_tree() as (_, repo, store):
            # Initialize the process hook once; concurrent sessions reuse it.
            self.run_demo("langchain", repo, store)
            with (
                patch(
                    "socket.socket.connect",
                    side_effect=AssertionError("Model network forbidden"),
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                values = list(
                    executor.map(
                        lambda _: self.run_demo("langchain", repo, store), range(2)
                    )
                )
            self.assertNotEqual(values[0][0], values[1][0])
            for _, document in values:
                self.assertEqual(len(document["observation"]["spans"]), 3)
            first = {span["span_id"] for span in values[0][1]["observation"]["spans"]}
            second = {span["span_id"] for span in values[1][1]["observation"]["spans"]}
            self.assertTrue(first.isdisjoint(second))

    def test_real_tool_secrets_and_error_remain_redacted_without_losing_agent_failure(
        self,
    ):
        from langchain_core.tools import tool

        secret = "sk-" + "a" * 48

        @tool
        def inspect_config(value: dict) -> dict:
            """Return the supplied synthetic configuration locally."""
            return value

        @tool
        def failing_tool() -> str:
            """Raise a synthetic credential-bearing error to verify capture does not echo it."""
            raise ValueError("api_key=" + secret)

        with workspace_tree() as (_, repo, store):
            with capture(
                "Inspect the configuration.",
                framework="langchain",
                repo_path=repo,
                storage_path=store,
            ) as recorded:
                inspect_config.invoke(
                    {"value": {"api_key": secret, "ordinary": "safe"}}
                )
            document = recorded.export()
            self.assertNotIn(secret, json.dumps(document))
            self.assertIn("safe", json.dumps(document))
            with (
                self.assertRaises(ValueError),
                capture(
                    "Inspect the configuration.",
                    framework="langchain",
                    repo_path=repo,
                    storage_path=store,
                ) as failed,
            ):
                failing_tool.invoke({})
            self.assertEqual(
                failed.export()["observation"]["execution_status"], "failed"
            )
            self.assertEqual(
                failed.export()["observation"]["spans"][0]["status"], "error"
            )
            self.assertNotIn(secret, json.dumps(failed.export()))

    def test_small_capture_budget_exposes_real_omission(self):
        from kuma.evidence.trace import TraceEvidenceLimits

        calls = module_file(
            "capture_budget_calls", ROOT / "examples/instrumented_observation.py"
        )
        with workspace_tree() as (_, repo, store):
            with capture(
                "Add 2 and 3.",
                framework="langgraph",
                repo_path=repo,
                storage_path=store,
                limits=TraceEvidenceLimits(max_spans=1),
            ) as recorded:
                self.assertEqual(calls.langgraph_call(), "5")
            observation = recorded.export()["observation"]
            self.assertNotEqual(observation["capture_status"], "complete")
            self.assertTrue(observation["truncated"])
            self.assertIn("trace_span_limit", observation["reasons"])
            self.assertGreater(observation["capture_summary"]["dropped_spans"], 0)

    def test_parent_outside_window_is_reported_as_context_gap_not_complete_capture(
        self,
    ):
        from kuma.capture_instrumentation import prepare_instrumentation

        calls = module_file(
            "capture_context_calls", ROOT / "examples/instrumented_observation.py"
        )
        provider, _ = prepare_instrumentation("langchain", None)
        with (
            workspace_tree() as (_, repo, store),
            provider.get_tracer("external-context-test").start_as_current_span(
                "existing-request"
            ),
        ):
            with capture(
                "Add 2 and 3.",
                framework="langchain",
                repo_path=repo,
                storage_path=store,
            ) as recorded:
                self.assertEqual(calls.langchain_call(), "5")
            observation = recorded.export()["observation"]
            self.assertEqual(observation["capture_status"], "failed")
            self.assertEqual(observation["spans"], [])
            self.assertIn("trace_topology_partial", observation["reasons"])
            with (
                self.assertRaises(ValidationError),
                patch("kuma.capture.KumaClient") as factory,
            ):
                judge_capture(recorded.capture_id, repo_path=repo, storage_path=store)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
