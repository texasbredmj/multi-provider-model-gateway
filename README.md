# Multi-Provider Model Gateway

## Start Here

[Launch the complete employer demonstration on GitHub](https://github.com/texasbredmj/ai-engineering-portfolio#start-here).

The public portfolio contains this project's runnable employer source, sample fixtures and verified demonstration, plus one Codespaces launch for all eight projects. No separate private-repository access is required. Run `python scripts/project_demo.py 6` from the portfolio Codespace to demonstrate this project.

The instructions below describe this repository's standalone version. The integrated platform and its current verification evidence are maintained in the portfolio.


Project 06 of AI Systems Engineering Lab provides authenticated, capability-aware model routing,
normalized responses, streaming, and Redis-backed usage budgets. Two local mock provider APIs use
different request/response formats. They exercise the gateway without a paid model endpoint or an
unreliable external dependency in required CI.

## Install and run

Requires Python 3.11+ and Docker Compose. From this repository on Windows:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock .
python -c "import secrets; from pathlib import Path; Path('.env').open('x').write('GATEWAY_API_KEY='+secrets.token_hex(24)+'\n')"
docker compose up -d --build
.venv/Scripts/python.exe scripts/demo.py
.venv/Scripts/python.exe scripts/verify_restart.py
$env:GATEWAY_INTEGRATION='1'
.venv/Scripts/python.exe -m pytest -q
```

Create `.env` only for the initial setup; the command refuses to overwrite an existing secret.
Never commit it. On Linux/macOS use `.venv/bin/python`, and prefix full tests with
`GATEWAY_INTEGRATION=1`. Defaults use Redis at `127.0.0.1:16379`, mock providers at `18670`, and
the gateway at `18671`. All exposed host ports are loopback-only. `docker compose down` stops
the stack and retains its Redis volume. Do not delete the volume if retained identities/budgets matter.

Without Redis, `python -m pytest -q -m 'not integration'` runs policy/contract tests. Skipped Redis
tests do not establish distributed admission or accounting correctness. Also run `ruff check .`,
`ruff format --check .`, and `python -m pip check`.

The demo checks real HTTP text, structured output, tool declarations, authorized fallback, timeout
handling, unsupported capabilities, budget rejection, successful streaming, and partial-stream failure.
It writes a new `.gateway/demo-<uuid>/report.json` on each run. The restart check restarts only the
gateway container and proves the response and usage are unchanged on replay.

## API

Every route except `/health` requires `Authorization: Bearer <GATEWAY_API_KEY>`.
`POST /v1/generate` also requires an `Idempotency-Key` of 8–64 letters, digits, or hyphens.

```json
{
  "model": "alpha-rate",
  "prompt": "Summarize the fixture",
  "mode": "text",
  "max_output_tokens": 32,
  "fallback_models": ["beta-text"],
  "max_cost_nusd": 1000000
}
```

Use `GET /v1/models` for the configured capability and price catalog and `GET /v1/usage` for the
current UTC day's accounted cost, pending reservations, reported tokens, completed attempts, and
unknown-usage attempts. Normal generation responses include request/model/provider IDs, normalized
output, reported usage, per-attempt latency/error/accounting, and total accounted cost for the request.
The top-level `usage` describes the successful final attempt; earlier attempts remain in `attempts`.

| Model profile | Capabilities | Context bound |
| --- | --- | --- |
| alpha-text | Text | 1,024 |
| alpha-pro | Text, fixed structured schema, fixed tool schema | 4,096 |
| beta-text | Text and text streaming | 8,192 |
| alpha-rate / alpha-unavailable / alpha-timeout | Text fault fixtures | 1,024 |
| beta-invalid / beta-stream-cut | Invalid usage / interrupted stream fixtures | 8,192 |

These are mock configurations, not capabilities or prices of real commercial models. Input context
and cost reservation use UTF-8 byte length as a conservative bound for these fixtures, plus the requested
output token maximum. Reported fixture token counts are whitespace-based test measurements, not a
production tokenizer. Real adapters would need provider-specific tokenization and billing contracts.

`mode=json` returns a validated `{answer: string, verified: boolean}` object. `mode=tools` requires
`tools=["lookup_stock"]` and returns one typed declaration for `{"sku":"demo-kit"}`. The gateway
does not execute tools or establish the factual truth of generated content. `state=SUCCEEDED` means
the provider response/usage contract validated, not that an external business task was completed.

## Routing, fallback, and budgets

```text
authentication → strict input + capability/context/model policy → Redis admission
→ reserve maximum attempt cost → configured provider adapter → validate/normalize
→ settle usage or retain unknown maximum → cache result + release active lease
```

There is no implicit fallback. The request may explicitly authorize at most one alternate model;
both selected models must satisfy the requested capability/context constraints before admission.
Timeouts, transport errors, invalid responses, 429s, and transient server failures can try that
authorized alternate within the remaining budget. Permanent provider rejections stop routing.
A 429 does not retry or repeatedly hit the same provider: the alternate may be tried immediately.
There are at most two model attempts. Each invocation has a two-second overall deadline and bounded
response size; incoming JSON is limited to 16 KiB with a two-second body deadline.

Redis command retries are disabled for ambiguous writes; reservation and settlement scripts also
deduplicate each model attempt so replay cannot double-charge or double-refund it.
Redis Lua scripts atomically coordinate a default four-request concurrency limit, a fixed UTC-minute
60-request admission limit, per-request budgets, and a default 10,000,000 nano-USD daily budget.
One USD is 1,000,000,000 nano-USD; integer accounting avoids float rounding. Prices are hypothetical
fixture inputs. No money is spent. Explicit pre-inference rejection refunds the full reservation;
valid usage refunds the unused portion. Timeouts, malformed responses, and incomplete streams retain
the maximum reservation because actual usage is unknown. Accounted cost is a conservative budget
figure, not a provider billing attestation. Missing token usage is never fabricated as known zero.

Redis retains request identity/results for 24 hours, daily aggregates for two days, and a capped audit
stream of roughly 1,000 attempts. Prompts/API keys are not stored in that audit; cached output is
retained for idempotent replay. Reusing a key with different input fails with 409. A pending key cannot
be replayed after cancellation/crash; its cost reservation stays visible. Active leases expire after
120 seconds so abandoned slots can be reclaimed without authorizing another inference for that key.
The provided Redis uses AOF with fsync on every write and no-eviction memory limits. This is a local
standalone Redis design, not Redis Cluster or an HA guarantee.

## Streaming

Set `stream=true` with a streaming-capable text model. Responses use SSE `delta`, then terminal
`done` or `error` events. Upstream beta NDJSON is parsed incrementally with a 64 KiB total bound.
Success requires validated final usage and clean completion. Missing/invalid usage after emitted
text produces `UNVERIFIED`, retains maximum cost, and never switches providers after partial output.
A stream's HTTP 200 only opens the channel; callers must read the terminal event. Cancellation,
Redis failure, or disconnect before completion can leave a pending request/reservation. Stream
identities are retained but not replayed as another generation; repeats return 409 with the prior result.

## Verification and limits

See [executed checks](docs/verification.md), [architecture](docs/architecture.md), and
[threat model](docs/threat-model.md). CI configures six Windows/Linux × Python 3.11/3.12/3.14 policy
jobs and one complete Redis/HTTP/restart job. Its artifacts retain test/demo evidence for 14 days.
Remote results are reported only after publication and independent confirmation.

The runtime intentionally permits only local mock-provider origins. No live vendor SDK, paid-provider
test, model judge, dynamic plugin, or arbitrary URL dispatch is enabled. A live adapter would require
an implementation, explicit provider configuration, credential isolation, reviewed capabilities/prices,
and separate cost authorization. Other limits include a single configured API client, fixed JSON/tool
schemas, no distributed tracing, and no storage-loss/HA or hostile-code isolation claim. Gateway outages
fail closed when Redis is unavailable. The project demonstrates routing policy, asynchronous API work,
atomic resource budgets, usage accounting, idempotency, and safe streaming failure handling.
