"""Offline regression checks for Authorization-header privacy classification."""

from __future__ import annotations

import json

from kuma.repository.privacy import (
    REDACTED,
    enforce_sensitive_policy,
    redact_sensitive_json,
    redact_sensitive_text,
    scan_sensitive_json,
    scan_sensitive_text,
)

ISSUE_98_EXAMPLES = (
    "Send `Authorization: Bearer <your-access-token>` with every request.",
    "Example: Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l   (RFC 7617 sample)",
    "Set the header to Authorization: Bearer YOUR_TOKEN_HERE before calling.",
    'curl -s -H "Authorization: Bearer $TOKEN" "https://scim.example.com/v2/Groups/..."',
)

SAFE_EXAMPLES = (
    "Authorization: Bearer <your-access-token>",
    "Authorization: Bearer YOUR_TOKEN_HERE",
    "Authorization: Bearer YOUR_ACCESS_TOKEN",
    "Authorization: Bearer $TOKEN",
    "Authorization: Bearer ${ACCESS_TOKEN}",
    "Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l",
)
PROSE_EXAMPLES = (
    "Work authorization: permitted",
    "Authorization: Statutory right-to-work checks",
    "Authorization: Statutory right-to-work checks are required before employment.",
    "Work authorization: permitted under applicable law.",
)
QUOTED_SAFE_EXAMPLES = tuple(
    f"Send {quote}{header}{quote} with every request."
    for quote in ('"', "'", "`")
    for header in SAFE_EXAMPLES
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


for example in (
    *ISSUE_98_EXAMPLES,
    *SAFE_EXAMPLES,
    *PROSE_EXAMPLES,
    *QUOTED_SAFE_EXAMPLES,
):
    findings = scan_sensitive_text(example, location="agent_output")
    assert not findings, example
    enforce_sensitive_policy(findings, allow_sensitive=False)
    assert redact_sensitive_text(example) == example, example

for example in ISSUE_98_EXAMPLES:
    payload = {"agent_output": example}
    findings = scan_sensitive_json(payload, location="payload")
    assert not findings, example
    enforce_sensitive_policy(findings, allow_sensitive=False)
    redacted_payload, changed = redact_sensitive_json(payload)
    assert not changed, example
    assert redacted_payload == payload, example
    assert payload == {"agent_output": example}, example
    assert not scan_sensitive_json(redacted_payload, location="payload"), example
    assert redact_sensitive_json(redacted_payload) == (payload, False), example

encoded_curl = json.dumps({"agent_output": ISSUE_98_EXAMPLES[3]})
assert not scan_sensitive_text(encoded_curl, location="encoded_curl")
assert redact_sensitive_text(encoded_curl) == encoded_curl
assert redact_sensitive_json(encoded_curl) == (encoded_curl, False)

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

for quote in ('"', "'", "`"):
    for header in (
        "Authorization: Bearer YOUR_TOKEN_HERE synthetic-credential",
        "Authorization: Bearer $TOKEN;synthetic-credential",
        "Authorization: Bearer <your-access-token> Basic synthetic-credential",
        "Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l synthetic-credential",
        "Authorization: Custom YOUR_TOKEN_HERE",
        "Authorization: Digest synthetic-credential",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.real.signature",
        "Authorization: Basic cHJpdmF0ZS11c2VyOnJlYWwtcGFzc3dvcmQ=",
        "Authorization: Basic yWxhZGRpbjpvcGVuc2VzYW1l",
    ):
        example = f"{quote}{header}{quote} trailing text"
        assert scan_sensitive_text(example, location="quoted_header"), example
        redacted = redact_sensitive_text(example)
        assert redacted == f"{quote}{REDACTED}", example
        assert not scan_sensitive_text(redacted, location="quoted_header"), example
        assert redact_sensitive_text(redacted) == redacted, example

    # Opening and closing delimiters must agree; dangling delimiters are not
    # evidence that a placeholder ends before the rest of the line.
    for example in (
        f"{quote}Authorization: Bearer YOUR_TOKEN_HERE",
        f"Authorization: Bearer YOUR_TOKEN_HERE{quote}",
        f"{quote}Authorization: Bearer YOUR_TOKEN_HERE{quote}synthetic-credential",
        *(
            f"{quote}Authorization: Bearer YOUR_TOKEN_HERE{other}"
            for other in ('"', "'", "`")
            if other != quote
        ),
    ):
        assert scan_sensitive_text(example, location="malformed_header"), example
        redacted = redact_sensitive_text(example)
        prefix = quote if example.startswith(quote) else ""
        assert redacted == f"{prefix}{REDACTED}", example
        assert not scan_sensitive_text(redacted, location="malformed_header"), example
        assert redact_sensitive_text(redacted) == redacted, example

    safe_header = f"{quote}Authorization: Bearer YOUR_TOKEN_HERE{quote}"
    digest_header = (
        'Authorization: Digest username="synthetic-user", '
        'response="0123456789abcdef0123456789abcdef"'
    )
    for example in (
        safe_header + " " + digest_header,
        digest_header + " " + safe_header,
    ):
        assert scan_sensitive_text(example, location="adjacent_digest"), example
        redacted = redact_sensitive_text(example)
        assert "synthetic-user" not in redacted, example
        assert "0123456789abcdef0123456789abcdef" not in redacted, example
        assert REDACTED in redacted, example
        assert not scan_sensitive_text(redacted, location="adjacent_digest"), example
        assert redact_sensitive_text(redacted) == redacted, example

    for tail, secret in (
        (" Authorization: Bearer synthetic-credential", "synthetic-credential"),
        (" Authorization: Custom synthetic-credential", "synthetic-credential"),
        (
            f" {quote}Authorization: Digest synthetic-credential{quote}",
            "synthetic-credential",
        ),
        (" api_key=synthetic-credential", "synthetic-credential"),
        (" password=synthetic-password", "synthetic-password"),
        (" ghp_0123456789abcdefghijklmnop", "ghp_0123456789abcdefghijklmnop"),
    ):
        example = safe_header + tail
        assert scan_sensitive_text(example, location="adjacent_secret"), example
        redacted = redact_sensitive_text(example)
        assert secret not in redacted, example
        assert REDACTED in redacted, example
        assert not scan_sensitive_text(redacted, location="adjacent_secret"), example
        assert redact_sensitive_text(redacted) == redacted, example

    for token in (
        "ghp_0123456789abcdefghijklmnop",
        "AKIA0123456789ABCDEF",
    ):
        example = f"{quote}Authorization: Bearer ${token}{quote}"
        assert scan_sensitive_text(example, location="placeholder_token"), example
        redacted = redact_sensitive_text(example)
        assert token not in redacted, example
        assert REDACTED in redacted, example
        assert not scan_sensitive_text(redacted, location="placeholder_token"), example
        assert redact_sensitive_text(redacted) == redacted, example

for prose in PROSE_EXAMPLES:
    example = prose + " password=synthetic-credential"
    assert scan_sensitive_text(example, location="prose_tail"), example
    redacted = redact_sensitive_text(example)
    assert "synthetic-credential" not in redacted, example
    assert REDACTED in redacted, example
    assert not scan_sensitive_text(redacted, location="prose_tail"), example
    assert redact_sensitive_text(redacted) == redacted, example

for example in (
    "Set the header to Authorization: Bearer YOUR_TOKEN_HERE before calling. synthetic-credential",
    "Example: Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l (RFC 7617 sample) synthetic-credential",
    "Authorization: Bearer YOUR_TOKEN_HERE with arbitrary explanatory prose.",
    "Authorization: Bearer YOUR_TOKEN_HERE before calling. Basic synthetic-credential",
):
    assert scan_sensitive_text(example, location="documentation_tail"), example
    redacted = redact_sensitive_text(example)
    assert redacted == example.split("Authorization:", 1)[0] + REDACTED, example
    assert not scan_sensitive_text(redacted, location="documentation_tail"), example
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
