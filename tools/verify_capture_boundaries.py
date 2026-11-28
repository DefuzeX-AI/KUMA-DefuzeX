"""Focused capture boundary regressions; external filesystem/import/model boundaries are explicit."""

import asyncio
import copy
import importlib
import io
import json
import os
import tempfile
import unittest
import warnings
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kuma import RawContentWarning, capture, judge_capture
from kuma.capture_cli import (
    _rules,
    cmd_capture_export,
    cmd_capture_judge,
    cmd_capture_list,
)
from kuma.capture_instrumentation import prepare_instrumentation
from kuma.capture_store import (
    MAX_CAPTURE_BYTES,
    _mkdir,
    _read,
    capture_directory,
    capture_id,
    encoded,
    list_captures,
    load_capture,
    output_target,
    save_capture,
    save_review,
    validate_capture,
)
from kuma.errors import ConfigurationError, ValidationError

core = importlib.import_module("kuma.capture")
store_module = importlib.import_module("kuma.capture_store")
wiring = importlib.import_module("kuma.capture_instrumentation")


class ModelContentBoundaryTests(unittest.TestCase):
    def test_indexed_metadata_is_not_a_body(self):
        from kuma.evidence.trace_model_content import map_model_content

        content, _, reasons = map_model_content(
            {"llm.output_messages.0.message.role": "assistant"}
        )
        self.assertEqual(content["output"], {"status": "not_recorded"})
        self.assertIn("trace_model_content_not_recorded", reasons)
        for attributes in (
            {"output.value": "null"},
            {"gen_ai.output.messages": ""},
            {"llm.output_messages.0.message.contents.0.message_content.text": ""},
            {"llm.output_messages.0.tool_call.function.name": "lookup"},
        ):
            with self.subTest(attributes=attributes):
                content, _, _ = map_model_content(attributes)
                self.assertEqual(content["output"]["status"], "present")


class CaptureBoundaryTests(unittest.TestCase):
    def test_media_mode_rejects_coercion_before_capture_or_transport(self):
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                core._review_inputs({}, None, None, accept_media_omission=value)

    @unittest.skipUnless(
        os.environ.get("KUMA_RUN_INSTRUMENTATION_TESTS") == "1",
        "Optional OTel matrix required for real raw capture lifecycle",
    )
    def test_explicit_raw_warning_lifecycle_persistence_and_cli(self):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
        self.addCleanup(provider.shutdown)
        tracer = provider.get_tracer("raw-synthetic")
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter("always")
            with (
                capture(
                    "Inspect synthetic@example.invalid",
                    tracer_provider=provider,
                    redact_sensitive=False,
                    **self.options,
                ) as session,
                tracer.start_as_current_span(
                    "model",
                    attributes={
                        "gen_ai.operation.name": "chat",
                        "input.value": '"synthetic@example.invalid"',
                        "output.value": '"done"',
                    },
                ),
            ):
                pass
        self.assertEqual(len(notices), 1)
        self.assertNotIn("synthetic@example.invalid", str(notices[0].message))
        with self.assertRaises(ValidationError):
            session.export()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RawContentWarning)
            raw = session.export(redact_sensitive=False)
            self.assertEqual(
                load_capture(
                    session.capture_id, redact_sensitive=False, **self.options
                )[0],
                raw,
            )
            forged = copy.deepcopy(raw)
            forged["observation"].pop("content_privacy")
            with self.assertRaises(ValidationError):
                validate_capture(forged, redact_sensitive=False)
            forged["content_privacy"] = "protected"
            with self.assertRaises(ValidationError):
                validate_capture(forged, redact_sensitive=False)
            for flag in (None, 0, "false"):
                with self.subTest(flag=flag), self.assertRaises(ValidationError):
                    capture("Inspect", redact_sensitive=flag, **self.options)
            _, key = core._review_inputs(raw, None, None, redact_sensitive=False)
            self.assertEqual(
                core._review_inputs(raw, None, None, redact_sensitive=False)[1], key
            )
            self.assertEqual(
                core._review_inputs(
                    raw, None, "manual-key-123", redact_sensitive=False
                )[1],
                "manual-key-123",
            )
            client = Mock()
            submission, _ = core._review_inputs(raw, None, None, redact_sensitive=False)
            client.trajectory.return_value.upload.return_value = {"id": "synthetic-id"}
            review = {"requirements_sha256": submission["requirements_sha256"]}
            client.trajectory.return_value.evaluate.return_value = review
            with patch.object(core, "save_review") as saved:
                self.assertEqual(
                    judge_capture(
                        session.capture_id,
                        client=client,
                        redact_sensitive=False,
                        **self.options,
                    ),
                    review,
                )
                saved.assert_called_once_with(
                    self.directory, review, redact_sensitive=False
                )
            client.trajectory.return_value.evaluate.return_value = {
                "requirements_sha256": "wrong"
            }
            with self.assertRaises(core.ProviderError):
                judge_capture(
                    session.capture_id,
                    client=client,
                    redact_sensitive=False,
                    **self.options,
                )
            with redirect_stdout(io.StringIO()) as output:
                cmd_capture_list(
                    Namespace(limit=20, no_redact_sensitive=True, **self.options)
                )
            self.assertNotIn("synthetic@example.invalid", output.getvalue())
            destination = self.root / "raw-export.json"
            cmd_capture_export(
                Namespace(
                    capture_id=session.capture_id,
                    output=destination,
                    no_redact_sensitive=True,
                    **self.options,
                )
            )
            self.assertEqual(json.loads(destination.read_text()), raw)

    def test_raw_cli_warning_precedes_rules_and_output(self):
        from kuma.cli import build_parser

        parser = build_parser()
        self.assertTrue(
            parser.parse_args(
                ["captures", "list", "--no-redact-sensitive"]
            ).no_redact_sensitive
        )
        with warnings.catch_warnings(), patch("kuma.capture_cli._rules") as rules:
            warnings.simplefilter("error", RawContentWarning)
            with self.assertRaises(RawContentWarning):
                cmd_capture_judge(Namespace(no_redact_sensitive=True))
            rules.assert_not_called()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Internal store helpers take canonical paths; public conformance retains raw aliases.
        self.root = Path(self.temporary.name).resolve()
        self.repo, self.store = self.root / "repo", self.root / "store"
        self.repo.mkdir()
        self.store.mkdir()
        with capture(
            "Inspect the object.",
            tracer_provider=object(),
            repo_path=self.repo,
            storage_path=self.store,
        ) as recorded:
            pass
        self.document = recorded.export()
        self.identifier = recorded.capture_id
        self.directory = self.store / ".kuma/captures"
        self.options = {"repo_path": self.repo, "storage_path": self.store}

    def test_explicit_worker_binding_lifecycle(self):
        session = capture("Inspect", tracer_provider=object(), **self.options)
        with self.assertRaises(ConfigurationError):
            session.bind(str)
        with session:
            bound = session.bind(str)
            self.assertEqual(bound(7), "7")
        with self.assertRaises(ConfigurationError):
            bound(8)
        with self.assertRaises(ConfigurationError):
            session.bind(str)

    def test_task_rules_reuse_and_detached_exports(self):
        for task in [None, "", " ", "x" * 12001]:
            with (
                self.subTest(task_type=type(task).__name__),
                self.assertRaises(ValidationError),
            ):
                capture(task)
        with self.assertRaises(ConfigurationError):
            capture("Inspect", framework="unknown")
        for length in [1501, 12000]:
            session = capture("x" * length, tracer_provider=object(), **self.options)
            with session:
                pass
            self.assertEqual(len(session.export()["requirements"]["objective"]), length)
        for limits in [object(), SimpleNamespace(max_total_bytes=1)]:
            with self.assertRaises(ConfigurationError):
                capture("Inspect", limits=limits)
        from kuma.evidence.trace import TraceEvidenceLimits

        with self.assertRaises(ConfigurationError):
            capture("Inspect", limits=TraceEvidenceLimits(max_total_bytes=1024))
        profile = self.document["requirements"]
        accepted = capture(profile["objective"], requirements=profile, **self.options)
        with self.assertRaises(ConfigurationError):
            accepted.export()
        with accepted:
            pass
        changed = accepted.export()
        changed["requirements"]["objective"] = "caller mutation"
        self.assertEqual(
            accepted.export()["requirements"]["objective"], profile["objective"]
        )
        with self.assertRaises(ConfigurationError):
            accepted.__enter__()
        self.assertFalse(accepted.__exit__(None, None, None))
        for task, tools in [("different task", None), (profile["objective"], [])]:
            with self.assertRaises(ValidationError):
                capture(task, requirements=profile, tools=tools)
        with capture(
            "Inspect.", tools=[], tracer_provider=object(), **self.options
        ) as declared:
            pass
        self.assertEqual(declared.export()["requirements"]["tools"], [])

    def test_async_cancellation_and_normal_completion_keep_original_status(self):
        recorded = capture("Inspect.", tracer_provider=object(), **self.options)
        cancelled = asyncio.CancelledError("original cancellation")

        async def run():
            try:
                async with recorded:
                    raise cancelled
            except asyncio.CancelledError as error:
                self.assertIs(error, cancelled)
                raise

        # A runner can reconstruct cancellation; identity is the SDK context's contract.
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run())
        self.assertEqual(
            recorded.export()["observation"]["execution_status"], "aborted"
        )
        self.assertEqual(recorded.persistence_status, "saved")

        async def normal():
            async with capture(
                "Inspect.", tracer_provider=object(), **self.options
            ) as session:
                pass
            return session

        self.assertEqual(asyncio.run(normal()).persistence_status, "saved")
        with patch.object(core, "save_capture", side_effect=OSError("private")):
            failed = capture("Inspect.", tracer_provider=object(), **self.options)

            async def saving_cancel():
                try:
                    async with failed:
                        raise cancelled
                except asyncio.CancelledError as error:
                    self.assertIs(error, cancelled)
                    raise

            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(saving_cancel())
            self.assertEqual(failed.persistence_error, "capture_save_failed")

    def test_external_default_storage_and_no_fallback(self):
        home = self.root / "home"
        home.mkdir()
        with patch.object(Path, "home", return_value=home):
            selected = capture_directory(self.repo, create=True)
            self.assertEqual(selected, home / ".kuma-captures/.kuma/captures")
            self.assertEqual(capture_directory(self.repo), selected)
            with self.assertRaises(ConfigurationError):
                capture_directory(home, create=True)
        for repo, storage in [
            (self.repo / "missing", self.store),
            (self.repo, self.repo),
            (self.repo, self.repo / "inside"),
            (self.repo, self.store / "missing"),
            ("\\\\unreachable.invalid\\share", self.store),
            (self.repo, "\\\\unreachable.invalid\\share"),
            (b"unsupported", self.store),
        ]:
            with self.subTest(), self.assertRaises(ConfigurationError):
                capture_directory(repo, storage, create=True)
        not_directory = self.root / "file"
        not_directory.write_text("data")
        with self.assertRaises(ConfigurationError):
            capture_directory(not_directory, self.store)
        for call in [
            lambda: capture_directory(self.repo, self.store / "none"),
            lambda: capture_directory(self.repo, self.store, create=True),
        ]:
            with (
                patch(
                    "kuma.capture_store.pinned_case_directory",
                    side_effect=OSError("private host path"),
                ),
                self.assertRaises(ConfigurationError) as error,
            ):
                call()
            self.assertNotIn("private host path", str(error.exception))
        entered = False
        with (
            patch(
                "kuma.capture_store._publish", side_effect=PermissionError("readonly")
            ),
            self.assertRaises(ConfigurationError),
            capture("Inspect.", tracer_provider=object(), **self.options),
        ):
            entered = True
        self.assertFalse(entered)
        with (
            patch.object(Path, "home", return_value=Path("//unreachable.invalid/home")),
            self.assertRaises(ConfigurationError),
        ):
            capture_directory(self.repo, create=True)
        with (
            self.assertRaises(ConfigurationError),
            output_target("\\\\unreachable.invalid\\share\\output.json"),
        ):
            self.fail("network output admitted")

    def test_posix_directory_descriptor_branch_uses_relative_names(self):
        @contextmanager
        def pinned(root, parent):
            yield 123

        original = os.mkdir

        def mkdir(name, mode=0o777, *, dir_fd=None):
            self.assertEqual(dir_fd, 123)
            self.assertEqual(name, "leaf")
            original(self.store / name, mode)

        with (
            patch("kuma.capture_store.pinned_case_directory", pinned),
            patch("kuma.capture_store.os.mkdir", mkdir),
        ):
            self.assertEqual(
                _mkdir(self.store, self.store, "leaf"), self.store / "leaf"
            )

    def test_windows_style_pinned_directory_and_output_branches(self):
        @contextmanager
        def pinned(root, parent):
            yield None

        with patch("kuma.capture_store.pinned_case_directory", pinned):
            self.assertEqual(
                _mkdir(self.store, self.store, "windows-leaf"),
                self.store / "windows-leaf",
            )
            with output_target(self.root / "windows-output.json") as publish:
                publish({"value": 1})
            self.assertEqual(
                json.loads((self.root / "windows-output.json").read_text()),
                {"value": 1},
            )

    def test_untrusted_capture_files_and_identity_are_rejected(self):
        for value in [None, "../escape", "cap_" + "a" * 31, "https://example.invalid"]:
            with self.assertRaises(ValidationError):
                capture_id(value)
        for change in [
            None,
            {},
            {**self.document, "schema_version": "other"},
            {**self.document, "unexpected": 1},
        ]:
            with self.assertRaises(ValidationError):
                validate_capture(change)
        for integration in [
            None,
            {},
            {"framework": "unknown", "status": "kuma_enabled"},
            {"framework": "existing", "status": "invented"},
            {"framework": [], "status": "application_owned"},
            {"framework": "existing", "status": []},
        ]:
            with self.assertRaises(ValidationError):
                validate_capture({**self.document, "instrumentation": integration})
        with (
            patch("kuma.capture_store.MAX_CAPTURE_BYTES", 1),
            self.assertRaises(ValidationError),
        ):
            validate_capture(self.document)
        with (
            patch("kuma.capture_store.scan_sensitive_json", return_value=["sensitive"]),
            self.assertRaises(ValidationError),
        ):
            validate_capture(self.document)
        path = self.directory / (self.identifier + ".json")
        original = path.read_bytes()
        for raw in [b"{", b'{"x":1,"x":2}', b"\xff", b" " * (MAX_CAPTURE_BYTES + 1)]:
            path.write_bytes(raw)
            with self.assertRaises(ConfigurationError):
                load_capture(self.identifier, **self.options)
        altered = copy.deepcopy(self.document)
        altered["capture_id"] = "cap_" + "a" * 32
        path.write_bytes(encoded(altered))
        with self.assertRaises(ValidationError):
            load_capture(self.identifier, **self.options)
        path.write_bytes(original)
        with self.assertRaises(ConfigurationError):
            save_capture(self.directory, self.document)
        with (
            patch(
                "kuma.capture_store.open_verified_log", side_effect=OSError("private")
            ),
            self.assertRaises(ConfigurationError) as error,
        ):
            _read(self.directory, path, 100)
        self.assertIsNone(error.exception.__context__)

    def test_lists_are_bounded_and_local_failures_safe(self):
        self.assertEqual(
            list_captures(**self.options)[0]["capture_id"], self.identifier
        )
        for limit in [True, 0, 101, "1"]:
            with self.assertRaises(ValidationError):
                list_captures(**self.options, limit=limit)
        with (
            patch.object(Path, "iterdir", side_effect=OSError("private")),
            self.assertRaises(ConfigurationError),
        ):
            list_captures(**self.options)

    def test_atomic_output_collision_permission_and_later_failure(self):
        output = self.root / "report.json"
        with output_target(output) as publish:
            publish({"source_time_ns": 1789783050660927701})
        self.assertEqual(
            json.loads(output.read_text())["source_time_ns"], 1789783050660927701
        )
        with self.assertRaises(ConfigurationError), output_target(output):
            self.fail("existing output admitted")
        with (
            patch(
                "kuma.capture_store._publish", side_effect=PermissionError("private")
            ),
            self.assertRaises(ConfigurationError),
            output_target(self.root / "denied.json"),
        ):
            self.fail("denied output admitted")
        target = self.root / "raced.json"
        with self.assertRaises(ConfigurationError), output_target(target) as publish:
            target.write_text("other user's result")
            publish({"value": 1})
        self.assertEqual(target.read_text(), "other user's result")
        with (
            patch.object(Path, "resolve", side_effect=OSError("private")),
            self.assertRaises(ConfigurationError),
            output_target(self.root / "missing.json"),
        ):
            self.fail("bad parent admitted")
        with (
            patch.object(Path, "is_symlink", return_value=True),
            self.assertRaises(ConfigurationError),
            output_target(self.root / "link.json"),
        ):
            self.fail("linked output parent admitted")

    def test_posix_output_probe_uses_pinned_directory_descriptor(self):
        @contextmanager
        def pinned(root, parent):
            yield 123

        published = []

        def publish(root, destination, value, descriptor):
            self.assertEqual(descriptor, 123)
            published.append(destination.name)

        with (
            patch("kuma.capture_store.pinned_case_directory", pinned),
            patch("kuma.capture_store._publish", publish),
            patch("kuma.capture_store.os.unlink") as unlink,
        ):
            with output_target(self.root / "descriptor.json") as writer:
                writer({"value": 1})
            unlink.assert_called_once_with(published[0], dir_fd=123)
            self.assertEqual(published[-1], "descriptor.json")

    def test_review_retention_deduplicates_and_detects_content_mismatch(self):
        value = {"result": 1}
        digest = save_review(self.directory, value)
        self.assertEqual(save_review(self.directory, value), digest)
        path = self.directory / ("review_" + digest + ".json")
        path.write_text("tampered")
        with self.assertRaises(ConfigurationError):
            save_review(self.directory, value)
        path.unlink()
        with (
            patch(
                "kuma.capture_store.save_observation",
                side_effect=ConfigurationError("denied"),
            ),
            self.assertRaises(ConfigurationError),
        ):
            save_review(self.directory, value)
        original = store_module.save_observation

        def race(data, destination, **kwargs):
            original(data, destination, **kwargs)
            raise ConfigurationError("concurrent publication")

        with patch("kuma.capture_store.save_observation", race):
            self.assertEqual(save_review(self.directory, value), digest)
        path.unlink()

        def bad_race(data, destination, **kwargs):
            Path(destination).write_text("tampered")
            raise ConfigurationError("concurrent publication")

        with (
            patch("kuma.capture_store.save_observation", bad_race),
            self.assertRaises(ConfigurationError),
        ):
            save_review(self.directory, value)
        with (
            patch("kuma.capture_store.scan_sensitive_json", return_value=["sensitive"]),
            self.assertRaises(ValidationError),
        ):
            save_review(self.directory, value)
        with self.assertRaises(ValidationError):
            save_review(self.directory, {"result": "a" * (8 * 1024 * 1024)})

    def test_optional_rules_and_cli_rejection_are_local(self):
        self.assertIsNone(_rules(None))
        path = self.root / "rules.json"
        path.write_bytes(encoded(self.document["requirements"]))
        self.assertEqual(_rules(str(path)), self.document["requirements"])
        for raw in [b"{", b"\xff", b"a" * 262145]:
            path.write_bytes(raw)
            with self.assertRaises(ValidationError):
                _rules(str(path))
        with self.assertRaises(ValidationError):
            _rules(str(self.root / "missing.json"))
        args = Namespace(
            **self.options,
            limit=20,
            capture_id=self.identifier,
            output=str(self.root / "export.json"),
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_capture_list(args), 0)
            self.assertEqual(cmd_capture_export(args), 0)
        from kuma.cli import build_parser

        parser = build_parser()
        self.assertEqual(parser.parse_args(["captures", "list"]).limit, 20)
        self.assertEqual(
            parser.parse_args(
                ["captures", "judge", self.identifier, "--output", "new.json"]
            ).timeout,
            180,
        )

    @unittest.skipUnless(
        os.environ.get("KUMA_RUN_INSTRUMENTATION_TESTS") == "1",
        "Optional framework matrix required",
    )
    def test_real_capture_explicit_review_and_cli_stopped_result(self):
        root = Path(__file__).resolve().parents[1]

        def load(name, path):
            import importlib.util

            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        verifier = load("capture_boundary_fixture", root / "tools/verify_capture.py")
        demo = load("capture_boundary_demo", root / "examples/capture_lifecycle.py")
        result = demo.run_sample("langchain", str(self.repo), str(self.store))
        document, _ = load_capture(result["capture_id"], **self.options)
        from kuma import KumaClient

        with verifier.judge_boundary(document["requirements"]) as (url, state):
            client = KumaClient(api_key="dfx_synthetic-trajectory-only", base_url=url)
            reviewed = judge_capture(
                result["capture_id"],
                client=client,
                idempotency_key="explicit-review-001",
                **self.options,
            )
            self.assertEqual(reviewed["review_status"], "completed")
            with patch.object(core, "KumaClient", return_value=client):
                self.assertEqual(
                    judge_capture(result["capture_id"], **self.options)[
                        "review_status"
                    ],
                    "completed",
                )
            before = len(state["requests"])
            for key in ["bad", "", True, "x" * 201]:
                with self.assertRaises(ValidationError):
                    judge_capture(
                        result["capture_id"],
                        client=client,
                        idempotency_key=key,
                        **self.options,
                    )
            wrong = copy.deepcopy(document["requirements"])
            wrong["objective"] = "changed task"
            with self.assertRaises(ValidationError):
                judge_capture(
                    result["capture_id"],
                    client=client,
                    requirements=wrong,
                    **self.options,
                )
            self.assertEqual(len(state["requests"]), before)
            with (
                patch(
                    "kuma.capture_store._publish",
                    side_effect=PermissionError("denied result store"),
                ),
                self.assertRaises(ConfigurationError),
            ):
                judge_capture(result["capture_id"], client=client, **self.options)
            self.assertEqual(len(state["requests"]), before)
            advanced = copy.deepcopy(document["requirements"])
            advanced["limitations"].append("Additional caller context.")
            self.assertNotEqual(
                core._review_inputs(document, None, None)[1],
                core._review_inputs(document, advanced, None)[1],
            )
            args = Namespace(
                **self.options,
                capture_id=result["capture_id"],
                output=str(self.root / "judged.json"),
                requirements=None,
                idempotency_key="cli-completed-01",
                reject_media=False,
                base_url=url,
                timeout=180,
            )
            with (
                patch.dict(
                    os.environ, {"KUMA_API_KEY": "dfx_synthetic-trajectory-only"}
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_capture_judge(args), 0)
            self.check_cli_collision(args, state)
        with verifier.judge_boundary(document["requirements"], stopped=True) as (
            url,
            _,
        ):
            args.base_url, args.output = url, str(self.root / "stopped.json")
            with (
                patch.dict(
                    os.environ, {"KUMA_API_KEY": "dfx_synthetic-trajectory-only"}
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cmd_capture_judge(args), 2)
            self.assertEqual(
                json.loads(Path(args.output).read_text())["review_status"], "stopped"
            )
        from kuma.errors import ProviderError

        workspace = Mock()
        workspace.upload.return_value = {"id": "synthetic-trace"}
        workspace.evaluate.return_value = {"requirements_sha256": "f" * 64}
        with self.assertRaises(ProviderError):
            judge_capture(
                result["capture_id"],
                client=SimpleNamespace(trajectory=lambda: workspace),
                **self.options,
            )

    def check_cli_collision(self, args, state):
        """Retain a paid response across a later output collision without changing its key."""
        args.output = str(self.root / "collision.json")
        args.idempotency_key = "cli-collision-01"

        def collide(*positional, **keywords):
            value = core.judge_capture(*positional, **keywords)
            Path(args.output).write_text("other result")
            return value

        with (
            patch.dict(os.environ, {"KUMA_API_KEY": "dfx_synthetic-trajectory-only"}),
            patch("kuma.capture_cli.judge_capture", side_effect=collide),
            redirect_stdout(io.StringIO()),
            self.assertRaises(ConfigurationError),
        ):
            cmd_capture_judge(args)
        self.assertEqual(Path(args.output).read_text(), "other result")
        self.assertTrue(list(self.directory.glob("review_*.json")))
        callbacks = state["callbacks"]
        args.output = str(self.root / "collision-recovered.json")
        with (
            patch.dict(os.environ, {"KUMA_API_KEY": "dfx_synthetic-trajectory-only"}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(cmd_capture_judge(args), 0)
        self.assertEqual(state["callbacks"], callbacks)


@unittest.skipUnless(
    os.environ.get("KUMA_RUN_INSTRUMENTATION_TESTS") == "1",
    "Optional OTel matrix required",
)
class InstrumentationBoundaryTests(unittest.TestCase):
    def instrumentor(self, flag=False):
        instrumentor = Mock()
        instrumentor.is_instrumented_by_opentelemetry = flag
        instrumentor.instrument.side_effect = lambda **kwargs: setattr(
            instrumentor, "is_instrumented_by_opentelemetry", True
        )
        return instrumentor

    def test_existing_missing_and_unknown_integrations(self):
        provider = object()
        self.assertIs(prepare_instrumentation("existing", provider)[0], provider)
        with self.assertRaises(ConfigurationError):
            prepare_instrumentation("unknown", provider)
        with (
            patch(
                "kuma.capture_instrumentation.importlib.import_module",
                side_effect=ImportError("private dependency detail"),
            ),
            self.assertRaises(ConfigurationError) as error,
        ):
            prepare_instrumentation("openai", None)
        self.assertIsNone(error.exception.__context__)
        self.assertNotIn("private dependency detail", str(error.exception))

    def test_owned_hook_reuse_different_provider_and_external_binding(self):
        provider = object()
        instrumentor = self.instrumentor(True)
        module = SimpleNamespace(OpenAIInstrumentor=lambda: instrumentor)
        with (
            patch.object(wiring, "_OWNED", {"openai": (provider, instrumentor)}),
            patch.object(wiring.importlib, "import_module", return_value=module),
        ):
            self.assertIs(prepare_instrumentation("openai", None)[0], provider)
            self.assertIs(prepare_instrumentation("openai", provider)[0], provider)
            with self.assertRaises(ConfigurationError):
                prepare_instrumentation("openai", object())
            instrumentor.instrument.assert_not_called()
            instrumentor.uninstrument.assert_not_called()
        with (
            patch.object(wiring, "_OWNED", {}),
            patch.object(wiring.importlib, "import_module", return_value=module),
        ):
            chosen, status = prepare_instrumentation("openai", provider)
            self.assertIs(chosen, provider)
            self.assertEqual(status["status"], "external_binding_unverified")
            instrumentor.instrument.assert_not_called()

    def test_new_hook_keeps_user_provider_and_checks_installation(self):
        instrumentor = self.instrumentor()
        provider = Mock()
        module = SimpleNamespace(OpenAIInstrumentor=lambda: instrumentor)
        with (
            patch.object(wiring, "_OWNED", {}),
            patch.object(wiring.importlib, "import_module", return_value=module),
        ):
            self.assertIs(prepare_instrumentation("openai", provider)[0], provider)
            instrumentor.instrument.assert_called_once_with(tracer_provider=provider)
            provider.shutdown.assert_not_called()
        instrumentor = self.instrumentor()
        module = SimpleNamespace(OpenAIInstrumentor=lambda: instrumentor)
        with (
            patch.object(wiring, "_OWNED", {}),
            patch.object(wiring.importlib, "import_module", return_value=module),
            self.assertRaises(ConfigurationError),
        ):
            prepare_instrumentation("openai", object())
        instrumentor.instrument.side_effect = None
        with (
            patch.object(wiring, "_OWNED", {}),
            patch.object(wiring.importlib, "import_module", return_value=module),
            self.assertRaises(ConfigurationError),
        ):
            prepare_instrumentation("openai", provider)


if __name__ == "__main__":
    unittest.main()
