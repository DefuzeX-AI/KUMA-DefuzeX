# Operation polling and deadlines

Official single-Case and Judge operations are polled with GET after the initial
idempotent POST. Polling starts immediately; subsequent waits use the start
response's `poll_after_ms` (1000 ms when resuming a known operation).

Active `queued`/`running` responses may include `poll_after_ms`: a strict integer
from 100 through 60000 milliseconds. It sets the next wait. If absent, the local
fallback doubles after each wait up to 8000 ms. Explicit server guidance can
exceed that fallback cap. Unknown fields, malformed intervals and intervals on
terminal envelopes remain invalid responses.

`operation_wait_timeout` is the total budget, including POST, GET and HTTP retry
backoff. `timeout` still bounds each HTTP attempt. A wait that would reach the
deadline reserves at most 100 ms (half the remaining budget if smaller) for one
last GET. No GET starts at or after the deadline. If the reserved poll is still
active or transiently fails, the SDK waits out the remaining budget and raises
retryable `operation_wait_timeout`; it does not spin or extend the deadline.

The reserve is an opportunity, not a promise to retrieve every result completed
before the deadline: scheduling, network latency and retries can consume it.
Timeout retains the original operation and idempotency identities. Explicitly
resume the existing request for GET-only recovery; do not create a new billable
operation. A response returned after the deadline is not accepted as success.

This reduces requests during long operations; completion detection may lag by
up to the current interval. It does not make the server finish sooner.

Offline check: `PYTHONPATH=src python tools/verify_operation_poll_backoff.py`.
The verifier uses synthetic wire responses and a fake clock, including the real
BackendClient deadline/retry path; it never calls a service or model.
