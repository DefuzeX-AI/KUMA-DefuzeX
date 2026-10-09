"""Offline checks for readable reports, safe errors and credential boundaries.

Run after installing this checkout: python tools/verify_safe_diagnostics.py.
Uses real SDK validators and synthetic public payloads, with no services, keys,
filesystem artifacts, private test imports or model calls.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy

from kuma.errors import ProviderError
from kuma.providers._official_judgment import normalize_official_judgment
from kuma.providers.official_case import _normalized_case
from kuma.repository.privacy import (
    redact_sensitive_json,
    redact_sensitive_text,
    scan_sensitive_json,
    scan_sensitive_text,
)
from kuma.transport.backend import _mapped_remote_error, _RemoteError


def public_case(prompt: str, title: str = "Offline example") -> dict:
    """Build a synthetic public Case with its actual canonical checksum."""
    case = {
        "schema_version": "2",
        "batch_id": "00000000-0000-4000-8000-000000000001",
        "case_id": "case-example",
        "strategy_id": "coding",
        "strategy_version": "1",
        "repo_fingerprint": "a" * 64,
        "title": title,
        "description": "One synthetic text input",
        "steps": [{"step_id": "step-1", "prompt": prompt}],
    }
    body = json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    case["signature"] = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    return {
        "batch": {
            "batch_id": case["batch_id"],
            "case_ids": [case["case_id"]],
            "strategy_id": "coding",
            "strategy_version": "1",
        },
        "cases": [case],
    }


def assessment(cause: str, reason: str) -> dict:
    """Provide a valid minimal negotiated assessment, without Evidence bodies."""
    axis = {
        "status": "unverifiable",
        "confidence": "low",
        "severity": "none",
        "evidence_refs": [],
        "reason_codes": [reason],
    }
    return {
        "schema_version": "kuma.judge_assessment.v1",
        **{
            key: deepcopy(axis)
            for key in ("task_completion", "artifact_quality", "behavioral_integrity")
        },
        "attributions": [{"cause": cause, "confidence": "low", "evidence_refs": []}],
        "claims": [],
        "claim_coverage": {
            "status": "unavailable",
            "reason_codes": ["not_assessed"],
            "reviewed_message_refs": [],
        },
    }


class SafeDiagnosticsTests(unittest.TestCase):
    """Guard four release surfaces through production normalization functions."""

    def test_local_explanations_preserve_findings_and_global_scope(self):
        wire = {
            "judgment_id": "judgment-example",
            "status": "insufficient_evidence",
            "issues": [
                {"issue_id": "issue-1", "severity": "low", "message": "Observed issue."}
            ],
            "evidence_gaps": [],
            "step_results": [
                {"step_id": "s1", "verdict": "insufficient_evidence", "issues": []},
                {"step_id": "s2", "verdict": "issue", "issues": ["issue-1"]},
                {"step_id": "s3", "verdict": "passed", "issues": []},
            ],
            "explanation": "UNTRUSTED_TEXT",
            "step_explanations": ["UNTRUSTED_TEXT"],
        }
        original = deepcopy(wire)
        baseline = normalize_official_judgment(wire)
        steps = baseline["extensions"]["step_explanations"]
        self.assertEqual([x["step_id"] for x in steps], ["s1", "s2"])
        self.assertIn("No reliably step-associated cause", steps[0]["message"])
        self.assertNotIn("UNTRUSTED_TEXT", json.dumps(baseline))
        self.assertEqual(wire, original)
        for cause, phrase in (
            ("environment", "environment"),
            ("input", "input"),
            ("model", "model"),
            ("instruction_conflict", "conflicting instructions"),
            ("unknown", "unknown cause"),
        ):
            for reason in ("missing_evidence", "partial_capture"):
                detailed = {**wire, "assessment": assessment(cause, reason)}
                report = normalize_official_judgment(
                    detailed, assessment_contract="kuma.judge_assessment.v1"
                )
                ext = report["extensions"]
                self.assertEqual(ext["assessment"], detailed["assessment"])
                self.assertEqual(ext["step_explanations"], steps)
                self.assertIn(phrase, ext["explanation"])
                self.assertIn(reason.replace("_", " "), ext["explanation"])
                self.assertIn("not step-specific", ext["explanation"])
                self.assertLess(len(ext["explanation"]), 1600)
                self.assertEqual(ext["step_results"], wire["step_results"])
                for field in ("status", "issues", "evidence_gaps"):
                    self.assertEqual(report[field], wire[field])
        for status in ("pass", "passed"):
            ext = normalize_official_judgment({**wire, "status": status})["extensions"]
            self.assertNotIn("explanation", ext)
            self.assertNotIn("step_explanations", ext)
        with self.assertRaises(ProviderError):
            normalize_official_judgment(
                {
                    **wire,
                    "assessment": assessment("UNTRUSTED_TEXT", "missing_evidence"),
                },
                assessment_contract="kuma.judge_assessment.v1",
            )

    def test_http_admission_details_are_closed_and_optional(self):
        for code, http_status, rule in (
            ("invalid_manifest", 422, "manifest_hash"),
            ("sensitive_content_detected", 400, "named_credential_assignment"),
        ):

            def mapped(extra, http_status=http_status, code=code):
                return _mapped_remote_error(
                    _RemoteError(
                        http_status,
                        {
                            "error": {
                                "code": code,
                                "retryable": False,
                                "message": "UNTRUSTED_TEXT",
                                **extra,
                            }
                        },
                    )
                )

            legacy = mapped({})
            details = {"rule": rule, "location": "logs", "file_index": 0}
            current = mapped({"details": details})
            self.assertEqual(current.code, code)
            self.assertEqual(dict(current.details), details)
            self.assertEqual(type(current), type(legacy))
            self.assertEqual(str(current), str(legacy))
            self.assertNotIn("UNTRUSTED_TEXT", str(current))
            self.assertFalse(current.retryable)
            for invalid in (
                {**details, "path": "UNTRUSTED_TEXT"},
                {**details, "file_index": True},
                {**details, "file_index": 1000},
                {**details, "location": "manifest"},
                {**details, "rule": "UNTRUSTED_TEXT"},
            ):
                with self.assertRaises(ProviderError) as caught:
                    mapped({"details": invalid})
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertNotIn("UNTRUSTED_TEXT", str(caught.exception))

    def test_generated_sensitive_case_is_not_caller_fault_or_modified(self):
        for prompt, expected in (
            ("Reply safely.", None),
            ("export PASSWORD=${PASSWORD}", None),
            ("export PASSWORD=synthetic-secret", "service_generated_sensitive_data"),
        ):
            wire = public_case(prompt)
            original = deepcopy(wire)
            kwargs = dict(
                repo_fingerprint="a" * 64,
                max_steps=10,
                requested_strategy_id="auto",
                requested_strategy_group=None,
            )
            if expected is None:
                result = _normalized_case(wire, **kwargs)
                self.assertEqual(
                    result["extensions"]["official_case"]["public_case"],
                    wire["cases"][0],
                )
            else:
                with self.assertRaises(ProviderError) as caught:
                    _normalized_case(wire, **kwargs)
                error = caught.exception
                self.assertEqual(error.code, expected)
                self.assertFalse(error.retryable)
                self.assertEqual(
                    dict(error.details),
                    {
                        "source": "service_generated_case",
                        "reasons": ("sensitive_content",),
                        "location": "public_case",
                    },
                )
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                self.assertNotIn("synthetic-secret", str(error))
            self.assertEqual(wire, original)
        with self.assertRaises(ProviderError) as caught:
            _normalized_case(public_case("Reply safely.", "x" * 201), **kwargs)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_braced_references_do_not_hide_literals_or_adjacent_secrets(self):
        for scalar in ("${PASSWORD}", "${_TOKEN_1}"):
            payload = {"password": scalar}
            self.assertFalse(scan_sensitive_json(payload, location="fixture"))
            self.assertEqual(redact_sensitive_json(payload), (payload, False))
            for value in (scalar, f'"{scalar}"', f"'{scalar}'"):
                safe = "password=" + value
                self.assertFalse(scan_sensitive_text(safe, location="fixture"))
                self.assertEqual(redact_sensitive_text(safe), safe)
        for value in (
            "${PASSWORD}literal",
            "${PASSWORD}}",
            "${PASSWORD:-synthetic-secret}",
            "${PASSWORD}${TOKEN}",
            "${password}",
        ):
            self.assertTrue(
                scan_sensitive_json({"password": value}, location="fixture")
            )
            unsafe = "password=" + value
            self.assertTrue(scan_sensitive_text(unsafe, location="fixture"))
            redacted = redact_sensitive_text(unsafe)
            self.assertFalse(scan_sensitive_text(redacted, location="fixture"))
            self.assertNotIn(value, redacted)
        mixed = 'password=${PASSWORD} "Authorization: Bearer $TOKEN" Authorization: Digest synthetic-secret'
        self.assertTrue(scan_sensitive_text(mixed, location="fixture"))
        redacted = redact_sensitive_text(mixed)
        self.assertNotIn("synthetic-secret", redacted)
        self.assertFalse(scan_sensitive_text(redacted, location="fixture"))


if __name__ == "__main__":
    unittest.main()
