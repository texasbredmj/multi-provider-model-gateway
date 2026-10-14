import asyncio
import hashlib
import json
import time

import httpx

from .contracts import MODELS, Generation, select


class GatewayError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def identity(key):
    return hashlib.sha256(key.encode()).hexdigest()


class Gateway:
    def __init__(self, ledger, adapter, allowed_models=None):
        self.ledger, self.adapter = ledger, adapter
        self.allowed_models = set(MODELS) if allowed_models is None else set(allowed_models)

    async def prepare(self, request: Generation, key):
        models = select(request)
        if any(name not in self.allowed_models for name, _ in models):
            raise GatewayError(403, "model is not authorized for this client")
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        rid = identity(key)
        claim = await self.ledger.claim(rid, fingerprint)
        if claim[0] == "cached":
            return rid, models, json.loads(claim[1])
        if claim[0] != "claimed":
            code = 429 if claim[0] in {"rate_limit", "concurrency"} else 409
            raise GatewayError(code, claim[0])
        return rid, models, None

    async def reserve(self, rid, name, model, request):
        maximum = (
            len(request.prompt.encode()) * model.input_nusd_per_token
            + request.max_output_tokens * model.output_nusd_per_token
        )
        day = await self.ledger.reserve(rid, maximum, request.max_cost_nusd, name)
        if not day:
            raise GatewayError(402, "cost budget cannot cover this attempt")
        return day, maximum

    async def settle(self, rid, name, model, day, maximum, started, error=None, usage=None):
        if usage is not None:
            actual = (
                usage.input_tokens * model.input_nusd_per_token
                + usage.output_tokens * model.output_nusd_per_token
            )
            classification = "reported_usage"
        elif error in {"rate_limited", "unavailable", "provider_rejected"}:
            actual = 0
            classification = "explicit_rejection"
        else:
            actual = maximum
            classification = "unknown_usage_reserved_maximum"
        attempt = {
            "model": name,
            "provider": model.provider,
            "error": error,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "usage": usage.model_dump() if usage else None,
            "accounted_nusd": actual,
            "accounting": classification,
        }
        total = await self.ledger.settle(
            rid,
            day,
            maximum,
            actual,
            (usage.input_tokens, usage.output_tokens) if usage else (0, 0),
            attempt,
        )
        return attempt, total

    @staticmethod
    def classify(exc):
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            return "timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            if exc.response.status_code == 429:
                return "rate_limited"
            if exc.response.status_code in {500, 502, 503, 504}:
                return "unavailable"
            return "provider_rejected"
        if isinstance(exc, httpx.TransportError):
            return "transport"
        return "invalid_response"

    async def generate(self, request, key):
        rid, models, cached = await self.prepare(request, key)
        if cached is not None:
            return cached
        attempts, total = [], 0
        result = None
        try:
            for name, model in models:
                day, maximum = await self.reserve(rid, name, model, request)
                started = time.perf_counter()
                usage, output, error = None, None, None
                try:
                    async with asyncio.timeout(2):
                        output, usage = await self.adapter.generate(model, request)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError) as exc:
                    error = self.classify(exc)
                attempt, total = await self.settle(
                    rid, name, model, day, maximum, started, error, usage
                )
                attempts.append(attempt)
                if error is None:
                    result = {
                        "http_status": 200,
                        "body": {
                            "request_id": rid,
                            "state": "SUCCEEDED",
                            "model": name,
                            "provider": model.provider,
                            "output": output,
                            "usage": usage.model_dump(),
                            "accounted_nusd": total,
                            "attempts": attempts,
                        },
                    }
                    break
                if error == "provider_rejected":
                    break
            if result is None:
                result = {
                    "http_status": 502,
                    "body": {
                        "request_id": rid,
                        "state": "FAILED",
                        "accounted_nusd": total,
                        "attempts": attempts,
                    },
                }
        except GatewayError as exc:
            result = {
                "http_status": exc.code,
                "body": {
                    "request_id": rid,
                    "state": "REJECTED",
                    "error": exc.message,
                    "accounted_nusd": total,
                    "attempts": attempts,
                },
            }
        # On cancellation, a process crash, or Redis failure, keep the request pending
        # and its pessimistic reservation. Never replay an ambiguous model invocation.
        await self.ledger.finish(rid, result)
        return result

    async def events(self, request, rid, models):
        attempts, total, emitted = [], 0, False
        for name, model in models:
            try:
                day, maximum = await self.reserve(rid, name, model, request)
            except GatewayError as exc:
                result = {
                    "request_id": rid,
                    "state": "REJECTED",
                    "error": exc.message,
                    "accounted_nusd": total,
                    "attempts": attempts,
                }
                await self.ledger.finish(rid, {"http_status": 409, "body": result})
                yield self.event("error", result)
                return
            started = time.perf_counter()
            usage, error = None, None
            try:
                async with asyncio.timeout(2):
                    async for kind, value in self.adapter.stream(model, request):
                        if kind == "delta":
                            if usage is not None:
                                raise ValueError("text after final usage")
                            emitted = True
                            yield self.event("delta", {"request_id": rid, "text": value})
                        elif usage is not None:
                            raise ValueError("duplicate final usage")
                        else:
                            usage = value
                if usage is None:
                    raise ValueError("stream ended without final usage")
            except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError) as exc:
                error = self.classify(exc)
                usage = None
            except asyncio.CancelledError:
                # Reservation and pending identity remain; caller can inspect usage.
                raise
            attempt, total = await self.settle(
                rid, name, model, day, maximum, started, error, usage
            )
            attempts.append(attempt)
            if error is None or emitted or error == "provider_rejected":
                state = "SUCCEEDED" if error is None else "UNVERIFIED"
                result = {
                    "request_id": rid,
                    "state": state,
                    "model": name,
                    "provider": model.provider,
                    "usage": usage.model_dump() if usage else None,
                    "accounted_nusd": total,
                    "attempts": attempts,
                }
                # Streaming identities are retained but never replayed as a second stream.
                await self.ledger.finish(rid, {"http_status": 409, "body": result})
                yield self.event("done" if error is None else "error", result)
                return
        result = {
            "request_id": rid,
            "state": "FAILED",
            "accounted_nusd": total,
            "attempts": attempts,
        }
        await self.ledger.finish(rid, {"http_status": 409, "body": result})
        yield self.event("error", result)

    @staticmethod
    def event(kind, value):
        return f"event: {kind}\ndata: {json.dumps(value)}\n\n"
