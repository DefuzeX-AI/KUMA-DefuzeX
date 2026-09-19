# Version and Release policy

`src/kuma/_version.py` is the version source; build metadata reads it dynamically.
Every candidate must pass independent review, public PR/CI and merge into public
main before receiving its own immutable version tag and GitHub Release. This document is policy,
not evidence that the candidate has shipped. Never move an existing tag or append
new functionality to an old release as a substitute for a new version.

## 0.3.0 release notes

These notes describe the `0.3.0` source release. The matching GitHub Release
and PyPI version establish publication; source metadata alone does not.
Install into the Agent's Python environment and restart it after upgrading.
Moving from 0.2.x to 0.3.0 produces a required-upgrade reminder under the policy
below, but never automatically installs software or blocks an Agent workload.

User-facing changes:

- Local `observe()` sessions, detached redacted export, readable timelines and
  explicit atomic saving without a Case, account, Judge or automatic upload.
- Optional authenticated cloud observation storage through explicit client
  calls, separate from local capture and evaluation.
- Bounded OpenInference model/tool projections and real, offline OpenAI,
  LangChain and LangGraph instrumentation examples.
- Optional external Run/invocation labels, validated server receipts and
  measured client-stage timings. Unknown server/model timings remain unknown.
- Reuse of an already successful identical Judge request within the same Run,
  preserving the original operation rather than starting another evaluation.

See [observation](observation.md), [cloud storage](cloud-observations.md),
[framework examples](instrumentation-examples.md) and
[Run correlation](run-correlation.md) for exact scope and limitations.
Local observation requires no KUMA account or service. Cloud observation and
official Run correlation require a server implementing their matching public
contracts, with the necessary authorization and capability advertisement.
An SDK upgrade does not enable those server features. Unavailable or unsupported
services fail explicitly; local observation remains usable without cloud storage.
No Agent runner, automatic instrumentation install, UI, guaranteed complete
capture, automatic package upgrade or service deployment is introduced.

The verified framework matrix records exact optional dependency versions and
known gaps; it is not a promise of support for every framework/version combination.

## Update classification

| Installed → latest stable | Status | Meaning |
| --- | --- | --- |
| 0.2.0 → 0.2.1 | optional | Patch-only, optional update |
| 0.1.9 → 0.2.0 | required | Higher minor, upgrade required reminder |
| 0.2.9 → 1.0.0 | required | Higher major, upgrade required reminder |
| 0.2.0 → 0.2.0 or 0.1.9 | up_to_date | Equal/ahead, no reminder |

These are KUMA's user-facing update rules, including pre-1.0 releases. Required
means a strong reminder, **not** automatic installation, interrupted work, denied
requests, or changed billing. Neither checks nor reminders retry paid operations.
Only strict `vX.Y.Z` stable Releases count; drafts, prereleases and main commits do not.

## API and CLI

`kuma updates check` and `kuma.check_for_updates()` accept no options/arguments.
They return/print a detached JSON-compatible mapping with exactly:

| Field | Type / Meaning |
| --- | --- |
| status | disabled / checking / unavailable / up_to_date / optional / required |
| current_version | Installed package's version string |
| latest_version | Validated stable version, or null when unavailable/disabled/checking |
| release_url | Fixed official GitHub tag URL derived from that version, or null |
| cached | boolean; true when using the process's cached success/failure |

Explicit calls may wait for one bounded read; when another check is in flight,
they immediately return checking without a new request. All listed CLI statuses
exit 0 rather than treating update availability as a business failure. Background
checks print optional/required reminders once to stderr, leaving JSON stdout intact.

Automatic scheduling occurs centrally after real official Backend transport
succeeds, covering Python and CLI. No import/help/local/custom check or blocking
join. Background threads are daemons: process exit does not wait, so short-lived
commands can end without a reminder. Use the explicit command for a result.

`KUMA_DISABLE_UPDATE_CHECK=1` is checked before cache access and before dispatch;
it disables explicit and automatic checks. Other values leave checks enabled.
Set it before starting a workflow; an HTTPS request already in flight cannot be
unsent, but no later reminder is printed after opt-out is observed.

The cache is one process-local entry, success/failure TTL 24 hours with monotonic
time, no disk storage. A new process starts a fresh cache. There is one request in
flight, no retry, a one-second socket timeout and 65536-byte response cap. Socket
timeout is not a hard wall-clock deadline for DNS/OS scheduling. No business
request waits on this check. Network/rate-limit/TLS/parse failures become safe
unavailable status, never raw exceptions or response bodies.

Only the fixed official GitHub releases/latest HTTPS endpoint is contacted.
No authentication, proxies or redirects; no API keys, Agent/Evidence/repository
data, local paths or original Backend headers are passed. GitHub sees ordinary
network metadata such as the connection's IP address. TLS trust uses Python's
default HTTPS verification; no arbitrary server-provided installation command
or URL is executed. The updater never runs pip or any subprocess.

## Historical 0.2.2 draft

- This historical draft standardized the localized `service_busy` message in
  sync/async errors without changing `ServiceBusyError`, code or retryable
  (including false). Current SDK-owned messages are English; this is not a
  current message contract.
- Raw server-internal text was not forwarded. Retry eligibility stayed unchanged;
  the draft did not authorize automatic retries, installation or PyPI publication.

## Historical 0.2.1 notes

- Counts Case and Evidence together per Judge item, including custom Cases;
  batch counts each item independently rather than summing all files.
- Consumes Backend max_files = supported maximum steps × 2 (currently 20),
  without hardcoding that value or using the current Run's actual step count.
  Deploy the matching Backend first; older advertised limits remain respected.
- Bytes, privacy and request identity remain unchanged. Backend enforces custom
  single Judge combined bytes; this patch adds no new local combined preflight.
- The 0.2.0 to 0.2.1 patch is optional, not an automatic installation or proof
  of PyPI publication.

## Historical 0.2.0 notes

- A distinct source release consolidates post-0.1.0 public features, including
  Agent Profile terminology, Case save/load, bounded Agent output/Trace, optional
  unified file diffs and captured tool argument/result Evidence. Verify each
  feature exists in the sanitized public candidate before publishing these notes.
- Adds optional/required version reminders and explicit CLI/Python checks with
  the offline, privacy and nonblocking boundaries described above.
- Users on 0.1.0 must upgrade manually once to gain update reminders, then restart
  their Agent; old code cannot acquire this behavior without an upgrade.
- No PyPI publication or service deployment is implied by a GitHub Release.

## Publication checklist

1. Update the single source version, API docs and bilingual README consistently;
   verify wheel/sdist metadata and installed package version agree.
2. Independently accept the private candidate and sanitized public scope; merge
   the exact accepted public PR only after required CI succeeds.
3. Create the **new** tag at the merged public commit, then create its independent
   Release with matching version, changes, compatibility notes and limitations.
   Preserve old tag targets and old release history.
4. Source installs may pin the matching release tag **only after that tag exists**. Main installs
   remain available for latest development source, not a promise of a new release.
5. Confirm the published stable Release is discoverable by the updater, without
   user credentials. Do not automatically upgrade user environments or publish PyPI.
