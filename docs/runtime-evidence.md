# Runtime Evidence v1 and v2

`Submission.extensions["runtime_evidence"]` is the canonical, bounded record of
runtime facts the SDK observed between one `get_input()` and its matching
`submit()`. Its closed schema is `defuzex.runtime_evidence.v1`; it does not
change the public Run method signatures. Local history and `save_local=True`
persistence continue to store this v1 form.

## Envelope and association

```json
{
  "schema_version": "defuzex.runtime_evidence.v1",
  "run_id": "run-...",
  "input_id": "step-1",
  "step_id": "step-1",
  "submission_id": "submission-...",
  "components": [
    {
      "component_id": "component-0000",
      "sequence": 0,
      "kind": "file_change",
      "path": "src/example.py",
      "change_type": "modified",
      "before_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "after_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
      "size_bytes": 120
    },
    {
      "component_id": "component-0001",
      "sequence": 1,
      "kind": "agent_response_claim",
      "claim_id": "submission-response",
      "claim": "completed",
      "text_sha256": "2222222222222222222222222222222222222222222222222222222222222222"
    }
  ]
}
```

The envelope contains exactly those six top-level fields. `run_id`, `input_id`,
`step_id`, and the stable `submission_id` bind it to one history item. It does
not contain `case_id`, `step_index`, an association map, or Case-generation
metadata. The outer Judge request owns `case_id` correlation.

`components` contains 1–100 items. `component_id` and non-negative `sequence`
are each unique, and sequence is strictly ascending. Serialization uses stable,
compact JSON. One encoded EvidenceItem is at most 120,000 characters.

## Closed component union

All components contain `component_id`, `sequence`, and `kind`. The remaining
fields are limited to:

| `kind` | Fields |
| --- | --- |
| `file_change` | `path`, `change_type` (`created`, `modified`, `deleted`, `unchanged`), optional `before_sha256`, `after_sha256`, `size_bytes` |
| `tool_call` | `tool_name`, `outcome` (`succeeded`, `failed`, `unknown`), required `arguments_sha256`, optional `result_sha256` |
| `command_result` | `command_id`, `exit_code`, optional `stdout_sha256`, `stderr_sha256` |
| `test_result` | `suite_id`, `outcome` (`passed`, `failed`, `partial`), `passed`, `failed`, `skipped` |
| `state_transition` | `state_id`, `outcome` (`succeeded`, `failed`, `unknown`), optional `before_sha256`, `after_sha256` |
| `artifact_snapshot` | `artifact_id`, optional `path`, `sha256`, `size_bytes`, `media_type` |
| `agent_response_claim` | v1: `claim_id`, `claim` (`completed`, `refused`, `blocked`), `text_sha256`; negotiated v2: a completed claim additionally requires `agent_output` |

The current framework-neutral SDK implementation always emits an
`agent_response_claim`. File tracking can emit `file_change`. Explicit log files
and configured in-process OTel capture can emit hash-only `artifact_snapshot`
items. Renames are represented as one delete and one create because `renamed`
is not part of the wire union.

The SDK currently has no public instrumentation that proves tool calls,
commands, tests, or state transitions, so it does not emit or declare those
kinds. An OTel span is an artifact observation, not proof of a `tool_call`.
Missing observations remain absent; the Judge decides whether that produces
`insufficient_evidence` for the Case's private capture requirements.

## Official transport and compatibility

Official Judge revalidates every envelope against its actual Run/Input/step and
stable Submission identity before upload. It sends one public `EvidenceItem`
per history item with:

- `source`: the negotiated `defuzex.runtime_evidence.v1` or
  `defuzex.runtime_evidence.v2`
- `media_type`: `application/vnd.defuzex.runtime-evidence+json`
- `content`: the canonical UTF-8 JSON above
- `name`: display-only filename

Transport is negotiated through the Backend's public Judge config. The SDK only
sends v2 when `evidence_types` explicitly contains
`defuzex.runtime_evidence.v2`; v2 wins when both versions are advertised. A
v1-only service receives the existing byte-compatible hash-only envelope. When
neither typed version is advertised, the SDK sends the existing
`defuzex.run_evidence.v1` item, so an older Backend never receives an unknown
schema. The SDK does not use legacy raw logs to emulate v2.

For a completed Submission, v2 adds exactly one field to its existing
`agent_response_claim`:

```json
{
  "kind": "agent_response_claim",
  "claim_id": "submission-response",
  "claim": "completed",
  "text_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
  "agent_output": {"answer": "the final Agent result"}
}
```

`agent_output` is the detached, finite JSON value accepted by `Run.submit()`.
It is the Agent's final response claim—not a tool result, model event, log,
trace span, prompt, completion, diff, or repository file. Failed, timed-out,
aborted, refused, and blocked claims never include it. Explicit `submit(output)`
still takes precedence over supported semantic OTel output extraction.

The output's canonical JSON is limited to 32,768 UTF-8 bytes. JSON is never
truncated because doing so could change its meaning or schema. Exceeding that
limit, the complete 120,000-character envelope limit, or dynamic Judge file and
total limits raises a stable `KumaError` before multipart POST. `text_sha256`
keeps the v1 algorithm: strings hash raw UTF-8 with `surrogatepass`; other values
hash key-sorted compact JSON with ASCII escapes and `allow_nan=false`.

For Official Case generation, `create_run()` derives `evidence_capabilities`
from the same configured capture boundaries. It only adds that optional public
wire field when `GET /sdk/entitlements/` contains
`protocol.casegen_frameworks = ["defuzex.casegen.ita.v1", ...]`; a missing,
malformed, or non-matching capability keeps the legacy request shape. Stable
ordering is `file_change`, `artifact_snapshot`, `agent_response_claim` for kinds
the Run can actually produce. The SDK never declares framework-only kinds.

## Privacy and resource behavior

V1 contains hashes instead of Agent output. V2 contains only the bounded final
Agent output described above; it does not add log bodies, trace bodies,
stdout/stderr, tool arguments, prompts, model responses, or diff text. Before a
v2 multipart POST, the SDK applies the existing canonical sensitive JSON scanner
to `agent_output` with no `allow_sensitive` bypass. Credential findings therefore
fail locally; the SDK stores neither the matched value nor raw diagnostic text.
Paths must be safe, root-relative, and pass the existing sensitive-path scanner.
Invalid, external, or sensitive observations are otherwise dropped before
serialization and reflected in the Submission's existing `missing` and
`dropped_count` fields.

Preparation, `save_local=True` persistence, log offsets, and trace exporter
state retain the existing transaction boundary: only an accepted Submission
commits them. Upload validation fails closed on extra fields, bad hashes,
duplicate IDs/sequences, malformed content, or association mismatch.

Generation packs, IT/Action selection, difficulty, injection data, behavior
oracles, private evaluation bundles, private rubrics, prompts, model settings,
and service credentials are outside this SDK contract and must never enter it.
