# Local trajectory review preparation

Trajectory review belongs to KUMA as an optional `kuma.trajectory` submodule, not a separate SDK.
Importing it adds no model client, tracing platform, account, API key or new package dependency.
Existing Run, Case generation, security Judge and OpenTelemetry behavior are unchanged.

The functions on this page prepare local materials without upload or charges. For the separate,
explicit authenticated workspace workflow available since 0.4.0, see
[trajectory workspace](trajectory-platform.md). Hosted use requires compatible enabled services;
these local preparation functions remain usable offline.

## Prepare an existing recorded run

```python
from pathlib import Path
from kuma.trajectory import encode_review, prepare_review

bundle = prepare_review(
    task="Inspect object dimensions. Keep the model unchanged.",
    events=[
        {
            "event": "tool_result",
            "turn_id": "turn-1",
            "tool": "inspect_object",
            "arguments": {"object": "hall"},
            "ok": True,
            "result": {"width": 8.0},
        }
    ],
    tools=[
        {
            "name": "inspect_object",
            "description": "Read the dimensions of an existing object.",
            "input_schema": {"type": "object"},
            "effects": "read_only",
        }
    ],
    sample="dimension-inspection",
    limitations=["No screenshot was exported."],
)
with Path("trajectory-bundle.json").open("xb") as stream:
    stream.write(encode_review(bundle))
```

The example is synthetic. It writes only the caller-selected local file and never executes the
recorded tool. Pass native exported event objects with their recorded `event` name, preserving
their order. This is an intake format, not a replacement event collector or a universal vendor-log
parser. Existing KUMA OTel capture and its privacy helpers remain the telemetry integration.

## Input and output contract

`prepare_review(task, events, *, sample="trajectory", tools=None, limitations=())` returns a
detached dictionary with schema `kuma.trajectory_review_bundle.v1`.

| Parameter | Accepted value and meaning |
| --- | --- |
| `task` | Required nonempty original task text, at most 12000 characters. Include relevant amendments; no private scoring rubric is required. |
| `events` | Required ordered list/tuple of 1..10000 JSON objects, each with a nonempty `event` string of at most 120 characters. Preserve recorded arguments, results, failures, turn/operation IDs and omissions; do not invent missing results. |
| `sample` | Nonempty local review label of at most 200 characters; defaults to `trajectory`. |
| `tools` | None for an unavailable catalog, or a list/tuple of at most 256 tool objects with distinct nonempty `name` strings of at most 200 characters. An empty list declares no available tools. Include descriptions, parameter/output schemas, prerequisites, effects and availability changes when known. |
| `limitations` | List/tuple of at most 32 known capture-gap strings, each nonempty and at most 1500 characters. Defaults to empty. |

All content passes the existing finite JSON and sensitive-content normalization boundary, with
depth 32 and a complete bundle limit of 4 MiB. Unsupported graphs, NaN, cycles and oversized data
raise `kuma.ValidationError` without echoing source values. Content is not silently truncated.
`content_status` is `present` or `redacted`; the latter remains visible after re-encoding.
Recognized credential values may be replaced or removed. This does not certify all private data
has been detected. Inspect the prepared contents before sharing them.

`encode_review(bundle)` accepts exactly the returned fields, revalidates and reapplies redaction,
then returns sorted compact UTF-8 JSON bytes. Caller edits cannot bypass the shared Evidence
boundary. Neither function mutates inputs, reads credentials, executes tools, performs I/O or
contacts a model. The caller owns file creation and any later external submission.

The bundle contains `schema_version`, `content_status`, `sample`, `task`, `events`, `tools` and
`limitations`. Hashing the encoded bytes gives a stable canonical input identity for review.

## Architectural boundary

- KUMA prepares and validates public input artifacts using its existing privacy boundary.
- Core owns process judgments, evidence verification, model access and independent-label comparison.
- Explicit hosted workspace calls use existing authentication and account infrastructure.
  They are separate from this local preparation API and need no new SDK credential.
- Human expected labels remain blank until separately reviewed. Models do not produce their own
  gold labels. Normal retries, missing evidence and provider failures are not automatic deductions.
