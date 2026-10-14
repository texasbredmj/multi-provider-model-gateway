"""Verify real loopback HTTP APIs. Read the ignored local secret without printing it."""

import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

root = Path(__file__).resolve().parents[1]
key = os.environ.get("GATEWAY_API_KEY")
if not key:
    key = next(
        line.split("=", 1)[1]
        for line in (root / ".env").read_text().splitlines()
        if line.startswith("GATEWAY_API_KEY=")
    )
origin = os.environ.get("GATEWAY_ORIGIN", "http://127.0.0.1:18671")
parsed = urlparse(origin)
if (
    parsed.scheme != "http"
    or parsed.hostname not in {"127.0.0.1", "localhost", "gateway"}
    or parsed.username
    or parsed.password
    or parsed.path not in {"", "/"}
    or parsed.query
    or parsed.fragment
):
    raise ValueError("demo gateway must be a local lab HTTP origin")
output = root / ".gateway" / ("demo-" + str(uuid.uuid4()))
output.mkdir(parents=True)
results = []
with httpx.Client(base_url=origin, timeout=12, trust_env=False) as client:
    until = time.monotonic() + 60
    while True:
        try:
            if client.get("/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > until:
            raise TimeoutError("gateway readiness deadline")
        time.sleep(0.2)
    assert client.get("/v1/models").status_code == 401
    for name, body, status in [
        ("text", {"model": "alpha-text", "prompt": "hello world"}, 200),
        ("structured", {"model": "alpha-pro", "prompt": "hello", "mode": "json"}, 200),
        (
            "tools",
            {"model": "alpha-pro", "prompt": "hello", "mode": "tools", "tools": ["lookup_stock"]},
            200,
        ),
        (
            "fallback",
            {"model": "alpha-rate", "prompt": "hello", "fallback_models": ["beta-text"]},
            200,
        ),
        (
            "timeout",
            {"model": "alpha-timeout", "prompt": "hello", "fallback_models": ["beta-text"]},
            200,
        ),
        ("unsupported", {"model": "alpha-text", "prompt": "hello", "stream": True}, 422),
        ("budget", {"model": "alpha-text", "prompt": "hello", "max_cost_nusd": 1}, 402),
    ]:
        headers = {"Authorization": "Bearer " + key, "Idempotency-Key": str(uuid.uuid4())}
        response = client.post("/v1/generate", json=body, headers=headers)
        assert response.status_code == status, (name, response.status_code, response.text)
        results.append({"scenario": name, "passed": True, "response": response.json()})
        if name == "text":
            assert client.post("/v1/generate", json=body, headers=headers).json() == response.json()
    for name, model, terminal in [
        ("stream", "beta-text", "done"),
        ("partial-stream", "beta-stream-cut", "error"),
    ]:
        headers = {"Authorization": "Bearer " + key, "Idempotency-Key": str(uuid.uuid4())}
        body = {"model": model, "prompt": "hello", "stream": True}
        if terminal == "error":
            body["fallback_models"] = ["beta-text"]
        lines = []
        started = time.perf_counter()
        first_delta = None
        with client.stream("POST", "/v1/generate", json=body, headers=headers) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if line.startswith("event: delta") and first_delta is None:
                    first_delta = time.perf_counter() - started
                lines.append(line)
        text = "\n".join(lines)
        assert text.count("event: delta") == 2 and "event: " + terminal in text
        results.append(
            {
                "scenario": name,
                "passed": True,
                "first_delta_seconds": first_delta,
                "duration_seconds": time.perf_counter() - started,
                "events": text,
            }
        )
    usage = client.get("/v1/usage", headers={"Authorization": "Bearer " + key}).json()
report = {
    "verified_demo": True,
    "scenario_count": len(results),
    "results": results,
    "usage_snapshot": usage,
    "network": "actual loopback HTTP gateway and two mock provider protocols",
    "paid_api_calls": 0,
}
(output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"verified_demo": True, "scenarios": len(results), "evidence": str(output)}))
