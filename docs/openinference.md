# OpenInference capture semantics

KUMA uses one bounded OTel span mapper. OpenInference is an additional source of
semantic attributes, not a new Agent runner or framework adapter. Capture does
not execute tools or models. An instrumentation package must actually record a
field before KUMA can retain it.

The mapped keys were checked against `openinference-semantic-conventions==0.1.38`
and the upstream [OpenInference semantic conventions](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md).
This is a bounded subset, not support for every attribute in that specification.

## Recorded identity and content

| Observed source | KUMA interpretation |
| --- | --- |
| `openinference.span.kind=TOOL` | `execute_tool`; `tool.name`, `input.value`, and `output.value` use existing tool metadata, arguments and result fields |
| `LLM` | `chat`; observed model name/provider/system and prompt/completion/total token counts map to existing `gen_ai` metadata |
| `AGENT` / `CHAIN` | `invoke_agent` / `invoke_workflow` identity; no invented final output |
| Other kinds | Unsupported attributes are excluded and counted, not guessed from span names |

Native `gen_ai.operation.name` takes precedence over OpenInference identity.
Native body fields also win over aliases; discarded aliases still contribute to
loss accounting. Parent IDs, links, status, start/end timestamps and duration
come from the real OTel span. `UNSET` is not success. Model tool-call proposals,
advertised schemas and tool definitions never establish that a tool ran.

For model spans only, the optional closed `model_content` object uses
`schema_version: kuma.model_content.v1` and two fields, `input` and `output`.
Each field has a `status`, and has `value` **only** for `present` or `redacted`.
A recorded JSON null is permitted and differs from a missing field.

Input/output source precedence is native `gen_ai.input.messages` /
`gen_ai.output.messages`, then OpenInference `input.value` / `output.value`,
then supported indexed `llm.input_messages.N.message.*` /
`llm.output_messages.N.message.*` attributes. Indexed messages preserve their
actual keys/indexes rather than filling gaps. Supported indexed fields are
`role`, `content`, `name`, `tool_call_id`, `function_call_name`, and
`function_call_arguments_json`, with indexes 0..127. Reasoning, images, arbitrary
metadata and unknown extensions are not blanket-allowlisted.

JSON-encoded strings are decoded at most once by the shared content normalizer;
ordinary text remains text. Malformed JSON-looking text is not represented as a
parsed object. Unsupported Python objects, cycles, non-finite numbers and graphs
deeper than 32 are rejected without object representations in diagnostics.

## Privacy, bounds and completeness

The shared Evidence sanitizer runs before captured tool/model content is stored.
Recognized credentials are replaced with `[REDACTED]`; safe surrounding content
is retained. Secret-bearing keys are omitted rather than renamed. Scanner rules
are not a guarantee of finding every unknown secret. No new detector or general
entropy heuristic is introduced here.

| Field status | Meaning |
| --- | --- |
| `present` | Actual recorded, bounded, validated content |
| `redacted` | Safe partial content retained after sensitive data removal |
| `not_recorded` | Unknown: instrumentation did not supply this field |
| `sensitive_content` | No safe field could be retained, or legacy wire cannot represent partial redaction |
| `size_limit` | Entire field omitted because of its bound |
| `invalid` | Malformed or unsupported value omitted |

Each body is limited to **4 MiB canonical JSON**, after redaction as well as
before. This is not an extra allocation of upload capacity: the existing 5 MiB
Runtime Evidence envelope, 8 MiB default Run Trace budget and dynamic multipart
limits still apply, including JSON/envelope overhead. Bodies are never cut into
fragments. Optional model fields are omitted whole, largest first with stable
identity/field tie-breaking, before existing evidence is discarded or an upload
is rejected. Each removed field becomes `size_limit`; capture stays partial with
correct body-drop counts and recomputed transport hashes. Immutable history is
not modified by upload fitting. If required non-model data alone exceeds limits,
the existing bounded failure still occurs locally before POST. Span sampling
and attribute/event limits remain active and report their existing reasons.

Redacted content is not a whole dropped body. Missing content is unknown, not
evidence that no tool was called. Metadata truncation, unsupported fields,
redaction and invalid/oversized omissions have distinct stable reason codes.
The existing aggregate drop count is not a fabricated per-category count.
Full retained topology can coexist with partial content; neither implies Judge
pass or independent proof of execution.

## Server compatibility

Model bodies are sent only when Judge configuration explicitly advertises
`trace_content_schemas: ["kuma.model_content.v1"]`. Absent or empty advertisement
keeps supported topology and legacy tool fields but omits model bodies with
`trace_attribute_not_allowlisted`, correct observed-drop accounting and partial
capture status. Local immutable capture is unchanged; transport hashes are
recomputed for the explicit projection after validating the original artifact.
An unknown advertised schema is rejected rather than guessed.

Retained redacted tool bodies additionally require the already-defined
`redaction` capability and component-scoped `redactions` annotation. Without
that support, the field becomes an explicit `sensitive_content` omission.
This change reuses the existing privacy primitives and Trace annotation only; it
does not implement or release the separate whole-Run redaction/opt-out feature.
Existing payloads without the new bodies retain their historical fields/hash.

## Instrumentation compatibility

See the [pinned framework examples](instrumentation-examples.md) for real offline
instrumentation checks. Semantic-key support alone is not a blanket guarantee
for every framework or package version.
