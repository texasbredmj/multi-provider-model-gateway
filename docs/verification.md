# Local verification — October 14, 2026

Project 06 is implemented locally. Publication and remote CI are a separate delivery checkpoint.
[Machine-readable evidence](evidence/verification-summary.json) preserves parsed JUnit results,
source hashes, HTTP demo responses, and restart checks. Required tests make zero paid-provider calls.

The complete suite has 43 tests, including actual standalone Redis integration. The policy-only
subset has 24 tests; skipping the remaining integration tests does not verify distributed budgets.
Windows uses Python 3.14.6 and a regular wheel in a clean virtual environment. Linux uses Python 3.12
and a regular wheel in a non-root Docker image, with test sources mounted read-only. Redis namespaces
are unique per test and cleanup deletes only those keys. Tests never flush the Redis database.

Coverage includes strict requests, model/context capabilities, explicit fallback, both provider wire
formats, structured/tool output contracts, API authorization, exact fixture cost arithmetic, request
and daily budgets, atomic concurrent admission, rate rejection, cache/conflict behavior across gateway
instances, timeout and malformed-usage accounting, pending cancellation, no replay of partial streams,
stream/request size bounds, fail-closed Redis behavior, and replay-safe reservation/settlement.

The nine-scenario real HTTP demo checks text, structured output, tool declarations, rate-limited
fallback, timeout fallback, unsupported capability rejection, budget rejection, successful SSE, and
partial SSE failure. It records first-delta and total stream timing, actual response envelopes,
terminal events, and a cumulative usage snapshot. The restart experiment restarts the gateway
container, replays the same identity, and verifies identical response and unchanged usage. It does
not test Redis destruction or HA recovery.

Commands include pinned wheel installation, `pytest` with `GATEWAY_INTEGRATION=1`, Ruff lint/format,
`pip check`, `docker compose up -d --build`, `python scripts/demo.py`, and
`python scripts/verify_restart.py`. Successful results are recorded only after execution.
Both body and provider-response limits are tested, including chunked inputs and streams without a
newline. The timeout fixture intentionally exceeds the gateway's actual two-second invocation deadline.
HTTPX in-process transports alone are not treated as network-timeout evidence; the real HTTP demo
also verifies that path.

Review of the installed Redis client identified automatic command retries as an accounting hazard.
They are disabled, and per-attempt reservation/settlement is idempotent; a regression test explicitly
replays both and verifies one charge/refund, one usage increment, and one audit entry.

The synthetic provider token counters and prices are test inputs. They do not attest commercial
model capabilities, provider tokenization, invoice accuracy, production latency, or factual quality.
Remote CI is configured for six OS/Python policy jobs and one Redis/HTTP/restart job; no remote
success is claimed until publication and artifact verification.
