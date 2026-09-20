# Explicit cloud observation history

These APIs are available in KUMA 0.3.0 or later.
[Local observation](observation.md) needs no key or network.
Cloud storage is an additional explicit action, never an automatic side effect
of observing or exporting. The server must enable this feature.

```python
from kuma import KumaClient, observe

# Your already configured instrumentation must produce spans around the Agent.
with observe(external_run_id="job-104") as capture:
    result = sum([2, 3])  # Replace with your real Agent call.
local_copy = capture.export()  # Still local: no upload or evaluation.
client = KumaClient()  # Uses KUMA_API_KEY or the credential file.
receipt = client.upload_observation(local_copy)  # Explicit opt-in POST.
page = client.list_observations(limit=20)
detail = client.get_observation(receipt["observation_id"])
assert detail["observation"] == local_copy
# Destructive cloud-storage choice; local copies remain:
# client.delete_observation(receipt["observation_id"])
```

Arithmetic alone generates no spans. Without compatible instrumentation the
capture truthfully reports unavailable/failed. Uploading such a capture is valid,
but does not turn it into complete evidence. Storage never creates Case/Judge,
calls a model or consumes evaluation credit. User Agent/provider costs are separate.

## Methods

All use `KumaClient(api_key=None, base_url=..., timeout=30.0)`: normal credential
resolution and positive per-request timeout in seconds. Each makes one HTTP
request without automatic retry/pagination. Returns are detached plain mappings.

| Method | Arguments | Return |
| --- | --- | --- |
| `upload_observation(observation)` | Required closed `capture.export()` mapping, finite JSON, at most 5 MiB canonical bytes. | Hash/ID/byte-bound `kuma.observation_receipt.v1`. |
| `list_observations(limit=20, cursor=None)` | Keyword-only strict integer 1-100; optional prior `next_cursor` opaque ID. | Metadata-only `kuma.observation_list.v1`: `items`, nullable `next_cursor`. |
| `get_observation(observation_id)` | Required `obs_` plus 32 lowercase hex characters. | `kuma.observation_detail.v1`: receipt and full sanitized observation. |
| `delete_observation(observation_id)` | Same required opaque ID; explicit destructive operation. | `kuma.observation_deleted.v1`: ID and `deleted: true`, even when absent. |

Every response also contains its exact `schema_version`. Receipt fields are
`observation_id`, `content_sha256`, `upload_status: accepted`,
`evaluation_status: not_performed`, UTC `created_at` and `cleanup_eligible_at`,
and `stored_bytes`. Hashing uses finite JSON, sorted keys, compact separators
and ASCII escaping. This binds consistency, not truth or authorization.
List items contain only receipt, execution/capture status, nullable `duration_ms`
and `sdk_version`; span bodies require an explicit detail read.

## Permissions and privacy

Upload/delete require explicit `sdk:observe`; list/detail require `sdk:read`.
Existing keys are not silently granted mutation rights. History belongs to the
authenticated **user within their tenant**, not one key: other authorized keys
of that same user can read retained context. There are no caller owner selectors.

Capture uses the existing redactor. Cloud admission defensively scans for known
sensitive content, without `allow_sensitive` bypass or silently rewriting export
bytes. Detection is not a guarantee against unknown secret formats. Detail may
contain private Agent context; review before sharing or saving it.

Invalid shape/graph/depth/ID/privacy/size raises
`ValidationError(code="observation_invalid")` before HTTP. Bad remote shape/hash
raises `ProviderError(code="invalid_response")`, without payloads in messages or
exception chains. Other failures retain standard `KumaError` code/retryable:
auth/scope, unavailable feature, conflict or capacity. No fallback to Judge.
Upload failure does not change the local export or Agent/evaluation outcome.

## Retry, capacity and deletion

Same user/ID/content retries return the original receipt without additional
capacity. Changed content under that ID conflicts. Keep the unchanged export to
retry explicitly. Each export is at most **5 MiB**, each retained tool/model value
at most **4 MiB**; the whole-export limit wins. No truncation occurs.
Server defaults are 1,000 records and 1 GiB canonical payload per tenant; metadata
overhead is separate. Full storage fails, never evicts or adds an evaluation fee.
Thirty days is cleanup eligibility, not an automatic deletion SLA. Explicit
deletion atomically reclaims owned capacity. Re-upload after deletion creates a
new record; there is no permanent replay promise across deletion.
