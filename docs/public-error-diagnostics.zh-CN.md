# Public error diagnostics

> This historical documentation path now contains the English guide. The
> [canonical guide](public-error-diagnostics.md) is maintained alongside it; the
> [Chinese overview](../README.zh-CN.md) remains available at the repository root.

This guide describes error display, not changes to request identity, billing or
retry decisions. Check [GitHub releases](https://github.com/DefuzeX-AI/KUMA-DefuzeX/releases)
and [PyPI](https://pypi.org/project/kuma-defuzex/) for published versions.

| Category | Codes | Meaning and action |
| --- | --- | --- |
| User input | invalid_request, strategy_group_invalid, unsupported_difficulty, strategy_capability_mismatch | Correct the named public field or selection using safe constraints. |
| Generated result | model_invalid_result; historical model_invalid_response | The service generated an unacceptable result. The user is not responsible for fixing that output. Optional reason distinguishes structure, type, range or format. |
| Generated policy/privacy | model_output_policy_conflict, model_output_privacy_rejected | The service output failed task requirements or safety checks; no private content or field path is exposed. |
| Model service | service_busy | A model-provider timeout, unavailability or actual busy response. |
| Internal service | request_failed | Safe server failure, not user input failure or user credit exhaustion. |
| Capacity / pending | capacity_exceeded, request_in_progress | Service admission capacity or an existing in-progress request, not evidence of model-provider trouble. |
| Historical unknown failure | operation_failed | The task failed, but the saved record cannot establish a more specific cause. ServiceError, without blaming the user or guessing model trouble. |

Authentication, permissions, quota, idempotency and not-found retain their distinct
codes. Program logic uses code/retryable, not translated messages. SDK messages
are fixed English text; arbitrary remote text is ignored. Historical
upstream_unavailable/upload_not_configured now use ServiceError, but preserve their
previous no-automatic-retry behavior. Known completed failures preserve their
original code and retryable value; the SDK does not rewrite stored history.

SDK-authored CLI/help, warnings, update reminders and example prompts are also
English. User-provided Chinese content is not translated. Chinese Agent Profile
heading aliases remain supported; an exact historical read-only-key message is
recognized as input but displayed in English. These retained semantic aliases
need an explicit policy resolution; they are not a repository-language exception.



## Official Judge display

For the official Judge only, `model_invalid_result`, historical
`model_invalid_response` and `service_busy` are displayed as
`ServiceBusyError` with code `service_busy` and the fixed message:

> Service is busy. Please try again later.

This display does not identify the underlying cause. It exposes neither remote
wording nor diagnostic details, and does not ask the user to repair generated
output. The original `retryable` flag and `request_id` are preserved.

This Judge-specific rule does not change Case-generation error classification
in the table above or errors from custom providers. It does not automatically
retry, create another paid task, rewrite stored history, or turn a failed task
into a successful report. See the [API reference](api-reference.md).

## Complete public error display

All codes in the service's frozen public error catalog have an explicit SDK class
or validation classification and fixed wording; arbitrary remote messages are not
used. In particular:

| Codes | SDK class | Meaning |
| --- | --- | --- |
| invalid_manifest, invalid_case_file, invalid_metadata, invalid_batch, invalid_log_parts, invalid_log_count, duplicate_log_name | ValidationError | The message identifies the malformed manifest, Case, metadata, batch or attachment relationship/count. |
| invalid_case_id, invalid_case_reference, invalid_client_request_id, invalid_idempotency_key, invalid_content_length | ValidationError | Correct the identified public reference or request field. |
| runtime_evidence_unsupported, unsupported_log_type, tool_capabilities_invalid | ValidationError | Unsupported Evidence format/capability or invalid tool declaration. |
| rate_limited | LimitExceededError | Request rate, not account credit exhaustion. Keep the received retryable flag; framework throttling can be false while dedicated rate limiting is true. |
| api_key_service_unavailable, strategy_catalog_unavailable | ServiceError | Authentication infrastructure or catalog unavailable; this does not mean the key or user input is wrong. |
| strategy_group_forbidden | PermissionDeniedError | The key cannot use the selected group. |
| strategy_catalog_changed | ValidationError | Refresh the catalog before choosing again. |
| sensitive_data_blocked, sensitive_content_detected | SensitiveDataError | Remove or redact sensitive input. |

HTTP and async failures use the same classification, even though async error
envelopes carry no HTTP status. Read-only-key/not-found and validated Case-step
limits retain their specialized messages. Unknown future codes still use a safe
generic fallback; the SDK does not expose private aliases or raw server text.

## Static input constraints

Each optional diagnostic record has field and reason. expected_type is a fixed
type name; minimum/maximum are static constraints, not observed user values.
String bounds count characters, array bounds count items, integer bounds are values.
Optional omitted constraints are not fabricated. At most 16 records are accepted.

| Public field | Type | Static constraints |
| --- | --- | --- |
| strategy_id, strategy_version | string | maximum 40 |
| count, max_steps | integer | minimum 1 |
| repo_meta, behavior_spec, manifest | object | none |
| agent_description | string | maximum 2000 |
| behavior_spec.production_scenario, behavior_spec.behaviors_to_test, behavior_spec.prohibited_behaviors | string | maximum 4000 |
| tool_list | array | maximum 100 |
| evidence_capabilities | array | minimum 1, maximum 7 |
| status | string | allowed_values exactly completed, failed, timeout, aborted |
| force, allow_sensitive | boolean | none |
| agent_name, agent_version | string | maximum 160 |
| repo_fingerprint | string | maximum 71 |
| case_sha256 | string | none |
| case_signature | string | maximum 255 |

Example: an invalid_request record for max_steps with reason=min_value,
expected_type=integer and minimum=1 prints a correction naming max_steps and its
minimum. It never prints the submitted value. Malformed or unknown details fail
with invalid_response; an async parsing failure leaves pending recovery intact.
No details from an older server means the generic safe message, not invented facts.
For invalid_request, model_invalid_result and historical model_invalid_response
only, the exact empty JSON object `details: {}` also means no known details, as
emitted by older servers. Null, lists and nonempty unknown shapes still fail closed.
This exception does not relax the required Case-step-limit details or other codes.

## Change notes

- Separate caller errors, service-generated failures and model availability.
- Display bounded static input constraints and four safe generated-result reasons.
- Preserve closed schemas, original retryable metadata and pending request identity.
- No automatic retry, new paid request or server-side historical rewrite is implied.
