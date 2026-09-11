# Save and reuse a complete Case

[简体中文](case-files.zh-CN.md) | English

Generate once, explicitly save the complete public Case, and execute it in a new
Run or process without another CaseGen request:

```python
from kuma import create_run

run = create_run(repo_path=".", agent_profile_path="agent-profile.md")
saved = run.save_case("case.json")  # new file; parent must exist
run.cancel()  # release this Run before starting another

# This can be a separate process. No Profile or Case Provider is needed.
reused = create_run(repo_path=".", case_path="case.json")
print(reused.case_origin)  # "official"
# Execute the Agent with reused.get_input(), then reused.submit(agent_output).
reused.cancel()  # omit this when continuing execution
```

The usual Docker/API-key rules still apply. For a trusted local environment use
`allow_local=True`; `judge=False` allows loading without official-service
credentials. Loading performs no CaseGen or strategy-catalog request. Official
Judge may negotiate Evidence and evaluate the saved Case; that is a separate
Judge request, not free evaluation or a promise of zero billing.

## File and identity

The UTF-8 file is the closed object `schema_version`, `origin`, `case`, `integrity`.
`schema_version` is `kuma.case_artifact.v1`; `origin` is `official` or `custom`.
Maximum size is 5,242,880 bytes; JSON is finite and bounded to depth 32 (root 0).
Duplicate keys and unknown fields fail. Files never contain Run IDs, execution
history, Evidence, Agent outputs, credentials or private Rubrics. Privacy
rejection applies even with `allow_sensitive=True`.

An official file contains its original public schema-2 Case exactly once:
`schema_version`, `batch_id`, `case_id`, `strategy_id`, `strategy_version`,
`repo_fingerprint`, `title`, `description`, `steps`, `signature`. Each step has
`step_id` and `prompt`. Integrity repeats the existing seven public references
and may include the validated `executed_strategy_group`. There is no second
normalized Inputs copy to edit independently.

A custom file contains exactly `case_id`, `input_type`, `input_schema`, `inputs`,
`extensions`, with `integrity: null`. Each input has `input_id`, `payload_type`,
`payload`, `public_constraints`, `extensions`. A custom Case remains custom even
when evaluated by official Judge. No Rubric is imported or created by the SDK.

Loading preserves Case ID, Input IDs/order and content; only the new Run ID is
rebound. `max_steps=None` uses the full saved count. An explicitly smaller value
fails; a larger one never adds steps. Do not also pass a Profile, Case Provider,
or non-default strategy. Automatic strategy scanning remains disabled.

## Integrity and official Judge

`signature` is `sha256:` plus canonical unsigned public-Case SHA-256;
`case_sha256` hashes the complete public Case including signature. Canonical
JSON uses sorted keys, compact separators, `ensure_ascii=False`, finite values
and UTF-8. **These are public checksums, not signatures proving server issuance.**
Changing a checksum cannot authorize a changed official Case.

New official Judge requests send only the original schema-2 object as multipart
`case_file`, filename `kuma-official-case.json`, MIME `application/json`; they do
not also send a top-level `case_id`. Existing integrity metadata remains bound
to the same object. Case bytes count toward negotiated file/total budgets.
Backend verifies the tenant-owned original and original Rubric before new Judge
reservation; accepted request recovery retains its original idempotency rules.

Older SDK objects missing the original public record and inputs-only JSON files
are not loadable official artifacts. Old Backend versions may reject the new
official file; the SDK surfaces that error and never retries as custom or
case-ID-only. To author a different Case, deliberately use a custom Provider and
save its resulting custom Case. Never remove `origin` or fake official integrity.

## Failure and file safety

`case_artifact_invalid` means malformed, oversized or changed content;
`case_origin_invalid` means absent/conflicting origin. Sensitive data is rejected
without returning its value. Missing/unreadable/unsafe paths and existing save
targets raise safe `ConfigurationError`. Relative paths use `repo_path`, not
process cwd. Parents must exist; links, mount escapes and overwrite are rejected.
Publication uses a temporary file, fsync and atomic no-replace hard link under a
pinned parent. A filesystem lacking that operation fails rather than overwriting.
Save/load never executes tools or uploads local configuration.
