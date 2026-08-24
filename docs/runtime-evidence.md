# Runtime Evidence v1

`Submission.extensions["runtime_evidence"]` is the canonical, bounded record of
runtime facts the SDK observed between one `get_input()` and its matching
`submit()`. Its closed schema is `defuzex.runtime_evidence.v1`; it does not
change the public Run method signatures.

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
| `agent_response_claim` | `claim_id`, `claim` (`completed`, `refused`, `blocked`), `text_sha256` |

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

- `source`: `defuzex.runtime_evidence.v1`
- `media_type`: `application/vnd.defuzex.runtime-evidence+json`
- `content`: the canonical UTF-8 JSON above
- `name`: display-only filename

Transport is negotiated through the Backend's public Judge config. The SDK only
sends typed items when `evidence_types` explicitly contains
`defuzex.runtime_evidence.v1`. Otherwise it sends the existing
`defuzex.run_evidence.v1` item, so an older Backend never receives an unknown
schema.

For Official Case generation, `create_run()` derives `evidence_capabilities`
from the same configured capture boundaries. It only adds that optional public
wire field when `GET /sdk/entitlements/` contains
`protocol.casegen_frameworks = ["defuzex.casegen.ita.v1", ...]`; a missing,
malformed, or non-matching capability keeps the legacy request shape. Stable
ordering is `file_change`, `artifact_snapshot`, `agent_response_claim` for kinds
the Run can actually produce. The SDK never declares framework-only kinds.

## Privacy and resource behavior

The envelope contains hashes instead of Agent output, log bodies, trace bodies,
stdout/stderr, tool arguments, prompts, or model responses. Paths must be safe,
root-relative, and pass the existing sensitive-path scanner. Invalid, external,
or sensitive observations are dropped before serialization and reflected in the
Submission's existing `missing` and `dropped_count` fields. Component and total
character limits are enforced while retaining the response claim.

Preparation, `save_local=True` persistence, log offsets, and trace exporter
state retain the existing transaction boundary: only an accepted Submission
commits them. Upload validation fails closed on extra fields, bad hashes,
duplicate IDs/sequences, malformed content, or association mismatch.

Generation packs, IT/Action selection, difficulty, injection data, behavior
oracles, private evaluation bundles, private rubrics, prompts, model settings,
and service credentials are outside this SDK contract and must never enter it.
