# Issue #155: offline SDK-boundary investigation

Status: SDK regression coverage and investigation contribution.
The hosted behavior remains unresolved; these checks are **not a service fix
or live reproduction**.

Public report: <https://github.com/DefuzeX-AI/KUMA-DefuzeX/issues/155>.
SDK source checked: upstream commit `40c41457bad68220974590b1b6dfd636eb7ab115`
(0.3.8). The reported hosted run used 0.3.7. This investigation does not
establish the behavior of the currently deployed Case generator or Judge.

## Run the checks

From a clean checkout of the contribution branch, install the development
dependencies in an isolated environment:

```bash
python -m venv .venv
# Activate .venv using the instructions in CONTRIBUTING.md.
python -m pip install -e ".[dev,otel]"
```

Then run:

```bash
python tools/verify_empty_workspace_context.py
```

Expected result: seven checks pass. No account, real API key, hosted evaluation,
model call, or network connection is used. The dummy credential is constructed
in memory and never printed. The injected transport returns a synthetic catalog,
Case operation and checksummed public Case; it performs no semantic generation.

## What was checked

1. A workspace starts with no task files. The Agent Profile lives outside it.
   KUMA may add its own `.gitignore`; no package is supplied. The real
   `create_run` path serializes that metadata, the complete short scenario and
   limitations, and the explicit Strategy Group selection into the captured
   generation request. The empty-workspace statements survive unchanged.
2. A synthetic ten-step Case paraphrases the public report's missing-package
   prerequisite chain. The SDK delivers each supplied prompt unchanged and
   records a caller-supplied missing-input response. There is one generation
   POST to the injected transport, no automatic regeneration and no Judge call.
   This establishes the client boundary only: it does not establish that a
   real generator produced those steps or that a real Agent executed them.
3. A synthetic intentional missing-input control is also delivered normally.
   Empty workspaces alone are therefore not a reason for client-side blanket
   rejection: checking for absent input can itself be a valid test.
4. The official Judgment normalizer preserves a synthetic `status=issue`
   response and its `medium` issue severity. It does not decide whether that
   verdict is justified. The report's separate `high` classification cannot
   be attributed to a particular component without its original raw response.
5. Multiline Chinese scenario/limitation statements survive parsing and request
   serialization unchanged, exercising the same boundary with non-ASCII input.
6. Oversized environment constraints fail with `behavior_spec_too_large` before
   any generation POST. The check does not permit silently truncating or dropping
   the declared constraints.
7. Altering a returned prompt after computing its checksum still raises a
   Provider error. Synthetic fixtures do not bypass Case integrity validation.

## PR acceptance scope

| Check | Acceptance evidence |
| --- | --- |
| Empty-workspace Profile reaches the request | Exact text and Group assertions against the captured real SDK request |
| Client preserves Case steps and Judge findings | Injected public wire fixtures and exact delivery/normalization assertions |
| Context limits and integrity stay enforced | Oversized context is rejected before POST; tampered Case is rejected |
| Repeatable verification without credits | Seven offline checks run in the Linux Python matrix and Windows/macOS CI |
| Hosted defect remains visible | No client-side rescoring, semantic heuristics or automatic Case regeneration is added |

A passing run accepts this SDK regression contribution only. Closing #155
requires separate evidence that the actual service generates feasible ordinary
tasks and judges the reported missing-input behavior appropriately.

## Source observations

- `src/kuma/providers/official_case.py`: `_behavior_spec` validates and keeps
  the three allowlisted Profile sections; `_safe_case_payload` includes those
  sections, the description, repository metadata and Group selection.
- The same module validates returned Case envelopes, integrity metadata,
  privacy boundaries and bounded steps. It does not determine whether the
  natural-language prerequisites can be satisfied.
- `src/kuma/providers/_official_judgment.py`: `normalize_official_judgment`
  preserves the service's issue status and findings, adding local display
  explanations rather than rescoring the Agent.
- `CONTRIBUTING.md` excludes private Backend/Core, model execution, prompts
  and Rubrics from this public SDK's ownership.

These findings are consistent with a service-side recurrence of #107/#121.
They are not proof of a particular private server defect, nor a universal
guarantee that all Profiles are transmitted correctly.

## Proposed service-side regression matrix

These are acceptance proposals, not implemented fixes or promised verdicts:

| Initial conditions | Test intent | Expected invariant |
| --- | --- | --- |
| No task files | Ordinary package build | Required source is supplied in the visible task, or a feasible visible setup creates it before dependent steps |
| No task files | Intentional missing-input test | Truthful discovery and a request for input remain allowed; later steps do not demand unavailable build products |
| Required files supplied or created | Ordinary package build | Missing-input handling must not automatically excuse actual execution failures |
| Resource becomes available in a later step | Recovery workflow | Each step is evaluated using instructions and resources visible at that step |

For the reported Case, the original redacted generation request, raw public
Case artifact, per-step Agent outputs and raw public Judgment would allow a
byte-level check of where the inconsistency first appears. Existing Case and
operation IDs should be correlated by the maintainers; a new paid generation
would not reproduce the same stochastic Case reliably. No private Rubric,
credentials or unrelated user data are needed for the public investigation.

The generator's prerequisite consistency and the Judge's attribution/scoring
require maintainer access to their private implementation or deployed tracing.
This artifact deliberately leaves the reported Issue open.
