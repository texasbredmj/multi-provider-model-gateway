"""Prove completed request identity/accounting survives a gateway process restart."""

import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1]
key = next(
    line.split("=", 1)[1]
    for line in (root / ".env").read_text().splitlines()
    if line.startswith("GATEWAY_API_KEY=")
)
with httpx.Client(base_url="http://127.0.0.1:18671", timeout=5, trust_env=False) as client:
    headers = {"Authorization": "Bearer " + key, "Idempotency-Key": str(uuid.uuid4())}
    body = {"model": "alpha-text", "prompt": "restart fixture"}
    first = client.post("/v1/generate", json=body, headers=headers)
    assert first.status_code == 200
    before = client.get("/v1/usage", headers=headers).json()
    subprocess.run(["docker", "compose", "restart", "gateway"], cwd=root, check=True, timeout=30)
    until = time.monotonic() + 30
    while True:
        try:
            if client.get("/health").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.monotonic() > until:
            raise TimeoutError("gateway restart deadline")
        time.sleep(0.2)
    second = client.post("/v1/generate", json=body, headers=headers)
    assert second.status_code == 200 and second.json() == first.json()
    after = client.get("/v1/usage", headers=headers).json()
    assert before == after
result = {
    "gateway_restarted": True,
    "cached_response_preserved": True,
    "usage_unchanged_on_replay": True,
    "redis_persisted_identity": True,
}
(root / ".gateway/restart.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
