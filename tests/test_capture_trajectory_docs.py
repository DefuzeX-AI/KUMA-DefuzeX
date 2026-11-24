"""Offline drift regressions for the capture/trajectory documentation boundary."""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import verify_public_api_docs as guard


class CaptureTrajectoryDocumentationTests(unittest.TestCase):
    def test_current_contracts_and_existing_entries_pass(self):
        self.assertEqual(len(guard.PUBLIC_APIS), 41)
        self.assertEqual(guard.main(), 0)

    def assert_drift(self, path, old, new, expected):
        """Mutate a disposable source/docs copy, never the candidate checkout."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {api.source for api in guard.PUBLIC_APIS}
            paths.update(str(p.relative_to(guard.ROOT)) for p in guard.DOCS)
            for item in paths:
                target = root / item
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(guard.ROOT / item, target)
            target = root / path
            text = target.read_text(encoding="utf-8")
            self.assertIn(old, text)
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            output = io.StringIO()
            docs = tuple(root / p.relative_to(guard.ROOT) for p in guard.DOCS)
            with (
                patch.object(guard, "ROOT", root),
                patch.object(guard, "DOCS", docs),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(guard.main(), 1)
            self.assertIn(expected, output.getvalue())

    def test_default_and_type_drift_fail(self):
        for new in ("limit: int = 21", "limit: float = 20"):
            with self.subTest(new=new):
                self.assert_drift(
                    "src/kuma/capture_store.py",
                    "limit: int = 20",
                    new,
                    "list_captures.limit type/default drift",
                )

    def test_parameter_name_and_docstring_drift_fail(self):
        self.assert_drift(
            "src/kuma/trajectory_client.py",
            "        dataset_id: str,",
            "        renamed_dataset: str,",
            "parameters",
        )
        self.assert_drift(
            "src/kuma/capture_store.py",
            "        limit: Integer",
            "        renamed_limit: Integer",
            "source Args for list_captures",
        )

    def test_missing_required_section_fails(self):
        self.assert_drift(
            "src/kuma/trajectory_client.py",
            "        Returns:\n            Always a dict",
            "        Outcome:\n            Always a dict",
            "run_experiment omits Returns",
        )

    def test_new_public_method_cannot_silently_escape_inventory(self):
        self.assert_drift(
            "src/kuma/trajectory_client.py",
            "    def request(",
            "    def added_public_method(self):\n        pass\n\n    def request(",
            "inventory drift",
        )

    def test_missing_and_duplicate_tables_fail(self):
        marker = "<!-- api-parameters:capture:start -->"
        self.assert_drift(
            "docs/api-reference.md",
            marker,
            "<!-- disabled-capture:start -->",
            "marker set",
        )
        self.assert_drift(
            "docs/api-reference.md",
            marker,
            marker + "\n<!-- api-parameters:capture:end -->\n" + marker,
            "duplicate marker capture",
        )
