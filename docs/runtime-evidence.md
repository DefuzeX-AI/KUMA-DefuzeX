# Runtime Evidence capabilities

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
compact JSON. One encoded EvidenceItem is at most 5 MiB of UTF-8 JSON.

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
items in historical schemas. With negotiated `runtime_trace`, the OTel artifact
also carries the actual filtered Trace body and completeness information; see
[Runtime Trace](runtime-trace.md). Renames are represented as one delete and one create because `renamed`
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

- `source`: the stable outer marker `defuzex.runtime_evidence.v1`
- `media_type`: `application/vnd.defuzex.runtime-evidence+json`
- `content`: the canonical UTF-8 JSON above
- `name`: display-only filename

Transport is negotiated through the Backend's public Judge config. A current
service advertises `defuzex.runtime_evidence.capabilities.v1`; the SDK then sends
an inner envelope with the exact ordered capabilities
`["runtime_evidence", "agent_output"]`. Historical inner v1 and v2 identifiers
remain compatibility inputs only. A v1-only service receives the existing
byte-compatible hash-only envelope, while a v2-only service receives its
historical output-bearing projection. When no typed schema is advertised, the
SDK retains the existing legacy upload for ordinary Runs. It never uses legacy
raw logs to emulate Agent output or file-diff support.

For a completed Submission, the `agent_output` capability adds exactly one field to its existing
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

The output's canonical JSON is limited to 4 MiB (4,194,304 UTF-8 bytes). JSON is never
truncated because doing so could change its meaning or schema. Exceeding that
limit, the complete 5 MiB Runtime Evidence limit, the 8 MiB SDK multipart limit,
or a stricter dynamic Judge limit raises a stable `KumaError` before multipart
POST. `text_sha256`
keeps the v1 algorithm: strings hash raw UTF-8 with `surrogatepass`; other values
hash key-sorted compact JSON with ASCII escapes and `allow_nan=false`.

For Official Case generation, `create_run()` derives `evidence_capabilities`
from the same configured capture boundaries. It only adds that optional public
wire field when `GET /sdk/entitlements/` contains
`protocol.casegen_frameworks = ["defuzex.casegen.ita.v1", ...]`; a missing,
malformed, or non-matching capability keeps the legacy request shape. Stable
ordering is `file_change`, `artifact_snapshot`, `agent_response_claim` for kinds
the Run can actually produce. The SDK never declares framework-only kinds.

## Optional file-diff capability

`create_run(upload_diff=True)` means “send safe unified patches to the Official
Judge when the service explicitly supports them.” It requires
`track_files=True`. The SDK adds `file_diff` as the third capability only when
the Backend advertises the named-capability schema. If the service does not
advertise it, the SDK raises `runtime_evidence_unsupported` before the Judge
multipart POST; it never silently falls back to hashes or `raw_log` after the
user explicitly requested diff upload.

Each `file_change` then contains exactly one of a closed `diff` object—format,
complete text, SHA-256, and exact UTF-8 byte count—or the content-free
`diff_omission_reason`: `binary`, `size_limit`, `sensitive_content`,
`no_text_change`, or `capture_incomplete`. Absolute local paths are rewritten to
validated repository-relative headers. The SDK never uploads a whole file and
never truncates a patch. Limits are 32,768 UTF-8 bytes per diff, 65,536 included
diff bytes per envelope, and 5 MiB for the complete Runtime Evidence envelope.

The final official multipart body, including framing and metadata, is capped at
8 MiB. Backend-advertised limits may be lower and remain authoritative. This
transport budget is separate from the 8 MiB default Trace budget accumulated
across one Run.

With the default `upload_diff=False`, `file_diff` is absent and file components
remain hash-only even on a current service.

## Privacy and resource behavior

V1 contains hashes instead of Agent output. The named `agent_output` capability
contains only the bounded final Agent output described above; it does not add
log bodies, trace bodies, stdout/stderr, tool arguments, prompts, or model
responses. Before multipart construction, the SDK applies the existing
canonical sensitive scanner to Agent output with no `allow_sensitive` bypass.
For an explicitly requested file diff, a sensitive match omits the text and
sends only `sensitive_content`; the rejected patch is also removed before Run
history and optional local Submission persistence. The SDK stores neither the
matched value nor raw diagnostic text in the upload.
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
