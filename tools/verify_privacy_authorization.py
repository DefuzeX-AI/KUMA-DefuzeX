"""Offline regression checks for Authorization-header privacy classification."""

from __future__ import annotations

import json

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
    'Authorization: Digest username="synthetic-user", response="0123456789abcdef0123456789abcdef"',
    "Authorization: Custom synthetic-credential",
    'Authorization: "Bearer synthetic-credential"',
    '"Authorization": "Digest synthetic-credential"',
    "Authorization: Bearer YOUR_TOKEN_HERE synthetic-credential",
    "Authorization: Bearer <your-access-token>; synthetic-credential",
    "Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l synthetic-credential",
    "Authorization: Bearer <synthetic-credential>",
    "Authorization: Bearer ${TOKEN synthetic-credential}",
    "Authorization: Bearer YOUR_TOKEN_HERE,synthetic-credential",
    "Authorization: Bearer",
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
    redacted = redact_sensitive_text(example)
    assert "synthetic-credential" not in redacted, example
    assert "synthetic-user" not in redacted, example
    assert "0123456789abcdef0123456789abcdef" not in redacted, example
    assert REDACTED in redacted, example
    assert not scan_sensitive_text(redacted, location="agent_output"), example
    assert redact_sensitive_text(redacted) == redacted, example

for scheme in ("Digest", "Custom", "Bearer", "Basic"):
    for value in (
        f"{scheme} synthetic-credential",
        {"scheme": scheme, "value": "synthetic-credential"},
        [scheme, "synthetic-credential"],
    ):
        example = json.dumps({"Authorization": value})
        assert scan_sensitive_text(example, location="structured")
        redacted = redact_sensitive_text(example)
        assert "synthetic-credential" not in redacted
        assert not scan_sensitive_text(redacted, location="structured")
        assert redact_sensitive_text(redacted) == redacted

multiline = "before\nAuthorization: Digest synthetic-credential\nafter"
assert redact_sensitive_text(multiline) == "before\n[REDACTED]\nafter"
print("privacy authorization verification passed")
