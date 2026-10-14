import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .adapters import Adapter
from .boundary import BodyLimit
from .contracts import MODELS, Generation
from .ledger import Ledger
from .service import Gateway, GatewayError


def create_app(api_key, ledger, adapter, allowed_models=None):
    if len(api_key) < 32:
        raise ValueError("GATEWAY_API_KEY must contain at least 32 characters")
    app = FastAPI(title="Multi-Provider Model Gateway", version="0.1.0")
    app.add_middleware(BodyLimit)
    gateway = Gateway(ledger, adapter, allowed_models)

    @app.middleware("http")
    async def policy(request: Request, next_handler):
        if request.url.path != "/health":
            supplied = request.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied.encode(), ("Bearer " + api_key).encode()):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return await next_handler(request)
        except RedisError:
            return JSONResponse(
                {"error": "usage ledger unavailable; request denied"}, status_code=503
            )

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        return JSONResponse({"error": exc.message}, status_code=exc.code)

    @app.exception_handler(ValueError)
    async def input_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.get("/health")
    async def health():
        return {"ready": bool(await ledger.redis.ping()), "mode": "local-mock-providers-only"}

    @app.get("/v1/models")
    async def models():
        return {
            name: asdict(model) for name, model in MODELS.items() if name in gateway.allowed_models
        }

    @app.get("/v1/usage")
    async def usage():
        return await ledger.usage()

    @app.post("/v1/generate")
    async def generate(body: Generation, request: Request):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", key):
            raise HTTPException(422, "Idempotency-Key must contain 8-64 letters, digits or hyphens")
        if body.stream:
            rid, selected, cached = await gateway.prepare(body, key)
            if cached is not None:
                return JSONResponse(
                    {
                        "error": "stream identity already consumed",
                        "previous_result": cached["body"],
                    },
                    status_code=409,
                )
            return StreamingResponse(
                gateway.events(body, rid, selected),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
        result = await gateway.generate(body, key)
        return JSONResponse(result["body"], status_code=result["http_status"])

    return app


def configured_app():
    redis = Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:16379/0"),
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
    )
    client = httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False)
    ledger = Ledger(redis)
    app = create_app(
        os.environ.get("GATEWAY_API_KEY", ""),
        ledger,
        Adapter(client, os.environ.get("PROVIDER_ORIGIN", "http://127.0.0.1:18670")),
    )

    @asynccontextmanager
    async def lifespan(app):
        await redis.ping()
        try:
            yield
        finally:
            await client.aclose()
            await redis.aclose()

    app.router.lifespan_context = lifespan
    return app
