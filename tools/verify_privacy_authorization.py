"""Offline regression checks for Authorization-header privacy classification."""

from __future__ import annotations

from kuma.repository.privacy import REDACTED, redact_sensitive_text, scan_sensitive_text

SAFE_EXAMPLES = (
    "Authorization: Bearer <your-access-token>",
    "Authorization: Bearer YOUR_TOKEN_HERE",
    "Authorization: Bearer YOUR_ACCESS_TOKEN",
    "Authorization: Bearer $TOKEN",
    "Authorization: Bearer ${ACCESS_TOKEN}",
    "Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l",
)
SECRET_EXAMPLES = (
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.real.signature",
    "Authorization: Basic cHJpdmF0ZS11c2VyOnJlYWwtcGFzc3dvcmQ=",
)


for example in SAFE_EXAMPLES:
    assert not scan_sensitive_text(example, location="agent_output"), example
    assert redact_sensitive_text(example) == example, example

for example in SECRET_EXAMPLES:
    findings = scan_sensitive_text(example, location="agent_output")
    assert {finding.kind for finding in findings} & {
        "authorization",
        "authorization_bearer",
    }
    assert redact_sensitive_text(example) == REDACTED, example

print("privacy authorization verification passed")
