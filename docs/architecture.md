# Architecture decisions

The gateway is independent of the other portfolio repositories. Pydantic contracts describe incoming
generation requests, normalized usage, a fixed structured response, and a fixed tool declaration.
A model catalog binds IDs to a provider protocol, fixed fixture route, capabilities, context bound,
and integer nano-USD prices. Callers can name configured models, not URLs or arbitrary provider options.

Alpha and beta deliberately have incompatible wire formats. Alpha sends prompt/limit/feature and
returns completion/usage; beta sends nested input/generation and returns result/metering or incremental
NDJSON. Adapters normalize these into the same gateway contract and validate usage before settlement.
Mock fixtures exercise protocol differences and errors without claiming real vendor compatibility.

Standalone Redis is the shared coordination boundary. Atomic scripts claim identities/admit requests,
reserve maximum cost, settle reported or unknown usage, and finalize cached results/release active
leases. Per-model reservation and settlement are idempotent within each request, including replayed ledger
commands. Automatic Redis command retries are disabled to avoid blindly replaying ambiguous writes.
No in-process counter is the distributed policy boundary. Redis server time determines minute
windows and UTC billing-day buckets. Gateway instances share the same configured namespace/client.
Tests use unique isolated namespaces and delete only those keys; they never flush the Redis database.
See [Redis atomic scripting](https://redis.io/docs/latest/develop/programmability/eval-intro/).

Provider dispatch and Redis cannot be one distributed transaction. A process can disappear after
reservation or inference but before settlement/finalization. Those states remain pending, retain the
reservation, and block replay. Slot expiry recovers capacity but cannot prove provider usage or justify
a refund. Results remain queryable through a repeated idempotency identity where finalized; daily
usage exposes pending costs. Retention is finite and a key can be reused after expiry; this is not an
indefinite duplicate-inference guarantee. Redis persistence is not a substitute for backups/HA.

Streaming validates raw byte limits before building NDJSON frames, preserving UTF-8 boundaries at
complete records. The gateway forwards deltas and requires one final usage record followed by clean
EOF. After any delta it will not mix in a different provider. Missing final usage/error/cancellation
cannot produce a verified `done`. HTTP status after headers cannot represent terminal stream outcome,
so callers must consume SSE events. Connections are closed with async context managers. See
[HTTPX async streaming](https://www.python-httpx.org/async/) and
[FastAPI StreamingResponse](https://fastapi.tiangolo.com/advanced/custom-response/).

Latency is measured per attempt with a monotonic clock. Token counts come from validated provider
metadata; fixture counts are intentionally simple. Accounting totals include conservative unknown
maximums, while reported tokens exclude unknown quantities. Costs are budget accounting in integer
nano-USD, not verified invoices. Audit events omit prompts, headers, and API keys; output caching
retains generated fixture content for 24 hours. The client authentication and catalog policy remain
backend-enforced for every endpoint except readiness.
