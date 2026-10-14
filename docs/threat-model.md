# Threat model and operational scope

The local operator controls model configuration, service environment, and the one client API key.
Client JSON, upstream outputs, usage, and streams are untrusted. Authentication precedes input
consumption. JSON/stream size bounds and deadlines limit ordinary oversized/slow inputs. Strict
schemas reject coercions, extra fields, undeclared tools, missing/invalid usage, and excess token
claims. Provider origins are fixed local HTTP hosts; redirects and ambient outbound proxies are
disabled. Model IDs/fallback lists cannot introduce arbitrary URLs or execute code.

Bearer authentication uses constant-time comparison. The gateway requires a generated secret with
at least 32 characters, never prints it, and does not forward that secret to providers. `.env` and
generated evidence directories are ignored by Git. API errors avoid upstream response-body/credential
disclosure. Generated outputs are cached for idempotency and may contain sensitive data in future
domains; retention/authorization would need review before such use.

Redis enforcement is server-side and atomic across gateway instances. Concurrency/rate/cost limits
do not rely on the caller or UI. If Redis fails, new requests are denied. Reservations survive
ambiguous failures and cannot silently become zero-cost successes. Cancellation may leave a pending
request and budget until retention expires; operators must reconcile external usage rather than
automatically replaying or refunding it. Unknown usage is conservatively accounted, not proven billing.

All Compose host ports bind loopback. The fixtures and Redis have no service authentication/TLS and
are intended for a trusted local single-operator network. No live vendor keys, production deployment,
multi-tenant identity management, Redis Cluster/HA, or arbitrary-agent sandbox is included. Anonymous
local access to Redis/provider ports remains an operator trust boundary. Do not expose this stack
publicly without a reviewed deployment/security design. Required tests perform no paid model calls.
