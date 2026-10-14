import asyncio
import json
import os
import uuid

import httpx
import pytest
from pydantic import ValidationError
from redis.asyncio import Redis

from model_gateway.adapters import Adapter, fixture_origin
from model_gateway.app import create_app
from model_gateway.contracts import Generation, select
from model_gateway.fixtures import app as providers
from model_gateway.ledger import Ledger
from model_gateway.service import Gateway, GatewayError, identity

KEY = "local-fixture-key-with-at-least-32-characters"


@pytest.mark.parametrize(
    "update",
    [
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        {"prompt": ""},
        {"prompt": " "},
        {"max_cost_nusd": -1},
        {"mode": "other"},
        {"extra": "field"},
        {"mode": "tools"},
        {"tools": ["lookup_stock"]},
        {"stream": True, "mode": "json"},
        {"fallback_models": ["alpha-text"]},
    ],
)
def test_strict_requests(update):
    with pytest.raises(ValidationError):
        Generation.model_validate({"model": "alpha-text", "prompt": "hello", **update})


@pytest.mark.parametrize(
    "model,mode,stream",
    [
        ("alpha-text", "json", False),
        ("alpha-pro", "text", True),
        ("beta-text", "tools", False),
        ("unknown", "text", False),
    ],
)
def test_unsupported_capabilities(model, mode, stream):
    req = Generation(
        model=model,
        prompt="hello",
        mode=mode,
        stream=stream,
        tools=["lookup_stock"] if mode == "tools" else [],
    )
    with pytest.raises(ValueError):
        select(req)


def test_context_bound():
    with pytest.raises(ValueError, match="context"):
        select(Generation(model="alpha-text", prompt="x" * 1024))
    assert select(Generation(model="beta-text", prompt="x" * 1024))


@pytest.mark.parametrize(
    "origin",
    ["https://example.com", "http://example.com", "http://user@localhost", "http://localhost/path"],
)
def test_external_origins_disabled(origin):
    with pytest.raises(ValueError):
        fixture_origin(origin)


def test_key_required():
    with pytest.raises(ValueError, match="API_KEY"):
        create_app("short", None, None)


def run_redis(check):
    if os.environ.get("GATEWAY_INTEGRATION") != "1":
        pytest.skip("requires local Redis; set GATEWAY_INTEGRATION=1")

    async def execute():
        redis = Redis.from_url(
            os.environ.get("REDIS_URL", "redis://127.0.0.1:16379/0"),
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        prefix = "gateway:test:" + str(uuid.uuid4())
        ledger = Ledger(redis, prefix)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=providers), base_url="http://providers"
        )
        adapter = Adapter(client, "http://providers")
        try:
            await redis.ping()
            await check(ledger, adapter)
        finally:
            keys = [k async for k in redis.scan_iter(prefix + "*")]
            if keys:
                await redis.delete(*keys)
            await client.aclose()
            await redis.aclose()

    asyncio.run(execute())


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["text", "json", "tools"])
def test_normalization_and_accounting(mode):
    async def check(ledger, adapter):
        request = Generation(
            model="alpha-pro",
            prompt="hello world",
            mode=mode,
            tools=["lookup_stock"] if mode == "tools" else [],
        )
        result = await Gateway(ledger, adapter).generate(request, "normal-0001")
        assert result["http_status"] == 200
        body = result["body"]
        assert body["usage"] == {"input_tokens": 2, "output_tokens": 4}
        assert body["accounted_nusd"] == 10000
        assert body["attempts"][0]["accounting"] == "reported_usage"
        assert body["attempts"][0]["latency_ms"] >= 0
        assert (await ledger.usage())["accounted_nusd"] == 10000
        audit = await ledger.redis.xrange(ledger.prefix + ":audit")
        assert audit[0][1]["request_id"] == body["request_id"]
        assert set(body["output"]) == {
            {"text": "text", "json": "object", "tools": "tool_calls"}[mode]
        }

    run_redis(check)


@pytest.mark.integration
@pytest.mark.parametrize(
    "model,error",
    [
        ("alpha-rate", "rate_limited"),
        ("alpha-unavailable", "unavailable"),
        ("alpha-timeout", "timeout"),
        ("beta-invalid", "invalid_response"),
    ],
)
def test_authorized_fallback(model, error):
    async def check(ledger, adapter):
        body = (
            await Gateway(ledger, adapter).generate(
                Generation(model=model, prompt="hello", fallback_models=["beta-text"]),
                "fallback-0001",
            )
        )["body"]
        assert body["state"] == "SUCCEEDED" and body["model"] == "beta-text"
        assert len(body["attempts"]) == 2 and body["attempts"][0]["error"] == error
        assert body["attempts"][1]["accounting"] == "reported_usage"
        if error in {"timeout", "invalid_response"}:
            assert body["attempts"][0]["accounting"] == "unknown_usage_reserved_maximum"

    # ASGI transports do not simulate network read timeouts; force the configured
    # timeout fixture to take longer than the gateway's actual two-second deadline.
    run_redis(check)


@pytest.mark.integration
def test_no_implicit_fallback_and_cached_failure():
    async def check(ledger, adapter):
        service = Gateway(ledger, adapter)
        req = Generation(model="alpha-unavailable", prompt="hello")
        first = await service.generate(req, "failure-0001")
        assert first["http_status"] == 502 and len(first["body"]["attempts"]) == 1
        assert first["body"]["accounted_nusd"] == 0
        assert await service.generate(req, "failure-0001") == first

    run_redis(check)


@pytest.mark.integration
def test_idempotency_and_conflict_across_gateway_instances():
    async def check(ledger, adapter):
        req = Generation(model="alpha-text", prompt="hello")
        first = await Gateway(ledger, adapter).generate(req, "same-key-0001")
        assert await Gateway(ledger, adapter).generate(req, "same-key-0001") == first
        assert (await ledger.usage())["output_tokens"] == 4
        with pytest.raises(GatewayError) as exc:
            await Gateway(ledger, adapter).generate(
                req.model_copy(update={"prompt": "different"}), "same-key-0001"
            )
        assert exc.value.code == 409

    run_redis(check)


@pytest.mark.integration
def test_atomic_concurrency_rate_and_pending_claims():
    async def check(ledger, adapter):
        ledger.concurrency = 1
        results = await asyncio.gather(*(ledger.claim(identity(str(i)), str(i)) for i in range(10)))
        assert sum(r[0] == "claimed" for r in results) == 1
        assert sum(r[0] == "concurrency" for r in results) == 9
        winner = next(i for i, r in enumerate(results) if r[0] == "claimed")
        assert (await ledger.claim(identity(str(winner)), str(winner)))[0] == "pending"
        await ledger.finish(identity(str(winner)), {"http_status": 200, "body": {}})
        ledger.rpm = 1
        assert (await ledger.claim(identity("next"), "next"))[0] == "rate_limit"

    run_redis(check)


@pytest.mark.integration
def test_request_and_daily_budget():
    async def check(ledger, adapter):
        service = Gateway(ledger, adapter)
        req = Generation(model="alpha-text", prompt="hello", max_cost_nusd=1)
        result = await service.generate(req, "budget-0001")
        assert result["http_status"] == 402 and not result["body"]["attempts"]
        assert (await ledger.usage())["accounted_nusd"] == 0
        ledger.daily_nusd = 1
        result = await service.generate(
            req.model_copy(update={"max_cost_nusd": 1000000}), "budget-0002"
        )
        assert result["http_status"] == 402

    run_redis(check)


@pytest.mark.integration
def test_model_policy_and_ineligible_fallback_before_admission():
    async def check(ledger, adapter):
        with pytest.raises(GatewayError) as exc:
            await Gateway(ledger, adapter, {"alpha-text"}).generate(
                Generation(model="beta-text", prompt="hello"), "denied-0001"
            )
        assert exc.value.code == 403
        with pytest.raises(ValueError):
            await Gateway(ledger, adapter).generate(
                Generation(
                    model="alpha-pro", mode="json", prompt="hello", fallback_models=["beta-text"]
                ),
                "denied-0002",
            )
        assert await ledger.redis.zcard(ledger.prefix + ":active") == 0

    run_redis(check)


@pytest.mark.integration
@pytest.mark.parametrize("model,expected", [("beta-text", "done"), ("beta-stream-cut", "error")])
def test_streaming_and_no_replay(model, expected):
    async def check(ledger, adapter):
        app = create_app(KEY, ledger, adapter)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            headers = {"Authorization": "Bearer " + KEY, "Idempotency-Key": "stream-0001"}
            data = {"model": model, "prompt": "hello", "stream": True}
            if model == "beta-stream-cut":
                data["fallback_models"] = ["beta-text"]
            result = await client.post("/v1/generate", json=data, headers=headers)
            assert result.status_code == 200
            assert result.text.count("event: delta") == 2
            assert "event: " + expected in result.text
            records = [
                json.loads(line[6:])
                for line in result.text.splitlines()
                if line.startswith("data: ")
            ]
            final = records[-1]
            assert len(final["attempts"]) == 1
            assert final["state"] == ("SUCCEEDED" if expected == "done" else "UNVERIFIED")
            if expected == "error":
                assert final["usage"] is None
                assert final["attempts"][0]["accounting"] == "unknown_usage_reserved_maximum"
            assert (
                await client.post("/v1/generate", json=data, headers=headers)
            ).status_code == 409
            assert await ledger.redis.zcard(ledger.prefix + ":active") == 0

    run_redis(check)


@pytest.mark.integration
def test_backend_authorization_api_and_usage():
    async def check(ledger, adapter):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(KEY, ledger, adapter)),
            base_url="http://gateway",
        ) as client:
            assert (await client.get("/v1/models")).status_code == 401
            headers = {"Authorization": "Bearer " + KEY}
            assert (await client.get("/v1/models", headers=headers)).status_code == 200
            assert (
                await client.post(
                    "/v1/generate", json={"model": "alpha-text", "prompt": "hello"}, headers=headers
                )
            ).status_code == 422
            headers["Idempotency-Key"] = "http-test-0001"
            response = await client.post(
                "/v1/generate", json={"model": "alpha-text", "prompt": "hello"}, headers=headers
            )
            assert response.status_code == 200
            assert (await client.get("/v1/usage", headers=headers)).json()["input_tokens"] == 1

    run_redis(check)


@pytest.mark.integration
def test_unknown_usage_retained_and_fallback_budget():
    async def check(ledger, adapter):
        req = Generation(
            model="beta-invalid",
            prompt="hello",
            fallback_models=["beta-text"],
            max_cost_nusd=100000,
        )
        result = await Gateway(ledger, adapter).generate(req, "unknown-0001")
        assert result["http_status"] == 402
        assert len(result["body"]["attempts"]) == 1
        assert result["body"]["accounted_nusd"] == 5 * 1500 + 32 * 2500
        assert (await ledger.usage())["accounted_nusd"] == result["body"]["accounted_nusd"]

    run_redis(check)


def test_redis_failure_denies_request():
    from redis.exceptions import ConnectionError

    class BrokenLedger:
        async def claim(self, *args):
            raise ConnectionError("fixture outage")

    async def check():
        app = create_app(KEY, BrokenLedger(), None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            result = await client.post(
                "/v1/generate",
                json={"model": "alpha-text", "prompt": "hello"},
                headers={"Authorization": "Bearer " + KEY, "Idempotency-Key": "redis-failure-0001"},
            )
            assert result.status_code == 503
            assert "denied" in result.json()["error"]

    asyncio.run(check())


@pytest.mark.parametrize("chunked", [False, True])
def test_input_body_limit(chunked):
    async def check():
        app = create_app(KEY, None, None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:

            async def chunks():
                yield b"x" * 10000
                yield b"x" * 10000

            content = chunks() if chunked else b"x" * 20000
            response = await client.post(
                "/v1/generate",
                content=content,
                headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
            )
            assert response.status_code == 413

    asyncio.run(check())


@pytest.mark.integration
def test_cancelled_request_keeps_reservation_and_blocks_replay():
    async def check(ledger, adapter):
        entered = asyncio.Event()

        class HangingAdapter:
            async def generate(self, *args):
                entered.set()
                await asyncio.sleep(30)

        req = Generation(model="alpha-text", prompt="hello")
        service = Gateway(ledger, HangingAdapter())
        task = asyncio.create_task(service.generate(req, "cancelled-0001"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await ledger.usage())["accounted_nusd"] == 69000
        assert (await ledger.usage())["pending_reserved_nusd"] == 69000
        with pytest.raises(GatewayError) as exc:
            await Gateway(ledger, adapter).generate(req, "cancelled-0001")
        assert exc.value.code == 409
        assert await ledger.redis.zcard(ledger.prefix + ":active") == 1
        await ledger.redis.zadd(
            ledger.prefix + ":active", {ledger.key(identity("cancelled-0001")): 0}
        )
        assert (await ledger.claim(identity("new-after-crash"), "fresh"))[0] == "claimed"

    run_redis(check)


@pytest.mark.integration
def test_stream_size_limit_before_newline():
    async def check(ledger, adapter):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 65537))
        ) as client:
            service = Gateway(ledger, Adapter(client, "http://providers"))
            req = Generation(model="beta-text", prompt="hello", stream=True)
            rid, selected, _ = await service.prepare(req, "large-stream-0001")
            output = "".join([event async for event in service.events(req, rid, selected)])
            assert "event: delta" not in output
            assert "event: error" in output
            assert "unknown_usage_reserved_maximum" in output

    run_redis(check)


@pytest.mark.integration
def test_replayed_reservation_and_settlement_are_idempotent():
    async def check(ledger, adapter):
        rid = identity("ledger-replay-0001")
        assert (await ledger.claim(rid, "fingerprint"))[0] == "claimed"
        day = await ledger.reserve(rid, 100000, 1000000, "alpha-text")
        assert await ledger.reserve(rid, 100000, 1000000, "alpha-text") == day
        assert (await ledger.usage())["accounted_nusd"] == 100000
        attempt = {"model": "alpha-text", "accounting": "reported_usage"}
        first = await ledger.settle(rid, day, 100000, 10000, (2, 4), attempt)
        assert await ledger.settle(rid, day, 100000, 10000, (2, 4), attempt) == first
        usage = await ledger.usage()
        assert usage["accounted_nusd"] == 10000 and usage["input_tokens"] == 2
        assert usage["pending_reserved_nusd"] == 0 and usage["attempts"] == 1
        assert await ledger.redis.xlen(ledger.prefix + ":audit") == 1

    run_redis(check)
