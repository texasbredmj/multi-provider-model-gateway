"""Two deliberately different provider protocols. Local deterministic fixtures only."""

import asyncio
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.get("/health")
async def health():
    return {"ready": True}


@app.post("/alpha/{behavior}")
async def alpha(behavior: str, body: dict):
    if behavior == "rate":
        raise HTTPException(429, "fixture rate limit", headers={"Retry-After": "0.01"})
    if behavior == "unavailable":
        raise HTTPException(503, "fixture unavailable")
    if behavior == "timeout":
        await asyncio.sleep(3)
    text = body["prompt"]
    output = {"text": "fixture answer"}
    if body["feature"] == "json":
        output = {"object": {"answer": "fixture answer", "verified": True}}
    elif body["feature"] == "tools":
        output = {"tool_calls": [{"name": "lookup_stock", "arguments": {"sku": "demo-kit"}}]}
    return {"completion": output, "usage": {"input_tokens": len(text.split()), "output_tokens": 4}}


@app.post("/beta/{behavior}")
async def beta(behavior: str, body: dict):
    usage = {"in": len(body["input"]["text"].split()), "out": 4}
    if behavior == "invalid":
        return {"result": {"message": "fixture answer"}, "metering": {"in": -1, "out": 99999}}
    if body.get("stream"):

        async def events():
            for word in ["fixture ", "answer"]:
                yield json.dumps({"delta": word}) + "\n"
                await asyncio.sleep(0.01)
            if behavior != "cut":
                yield json.dumps({"metering": usage}) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")
    return {"result": {"message": "fixture answer"}, "metering": usage}
