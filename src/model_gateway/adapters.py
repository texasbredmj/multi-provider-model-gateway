import json
from urllib.parse import urlparse

import httpx

from .contracts import Structured, ToolCall, Usage


def fixture_origin(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "providers"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("only local mock provider origins are enabled")
    return url.rstrip("/")


class Adapter:
    def __init__(self, client: httpx.AsyncClient, origin: str):
        self.client = client
        self.origin = fixture_origin(origin)

    def arguments(self, model, request):
        if model.provider == "alpha":
            return {
                "prompt": request.prompt,
                "limit": request.max_output_tokens,
                "feature": request.mode,
            }
        return {
            "input": {"text": request.prompt},
            "generation": {"max_new_tokens": request.max_output_tokens},
            "stream": request.stream,
        }

    def url(self, model):
        return f"{self.origin}/{model.provider}/{model.route}"

    async def generate(self, model, request):
        async with self.client.stream(
            "POST", self.url(model), json=self.arguments(model, request)
        ) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 65536:
                    raise ValueError("provider response exceeds budget")
                chunks.append(chunk)
            body = json.loads(b"".join(chunks))
        if model.provider == "alpha":
            usage = Usage.model_validate(body["usage"])
            output = body["completion"]
        else:
            usage = Usage(
                input_tokens=body["metering"]["in"], output_tokens=body["metering"]["out"]
            )
            output = {"text": body["result"]["message"]}
        if (
            usage.input_tokens > len(request.prompt.encode("utf-8"))
            or usage.output_tokens > request.max_output_tokens
        ):
            raise ValueError("provider usage exceeds reservation")
        if request.mode == "json":
            output = {"object": Structured.model_validate(output["object"]).model_dump()}
        elif request.mode == "tools":
            calls = output["tool_calls"]
            if len(calls) != 1:
                raise ValueError("exactly one declared tool call is required")
            output = {"tool_calls": [ToolCall.model_validate(c).model_dump() for c in calls]}
        elif (
            set(output) != {"text"}
            or not isinstance(output["text"], str)
            or len(output["text"]) > 8192
        ):
            raise ValueError("invalid text output")
        return output, usage

    async def stream(self, model, request):
        size = 0
        pending = b""
        async with self.client.stream(
            "POST", self.url(model), json=self.arguments(model, request)
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 65536:
                    raise ValueError("provider stream exceeds budget")
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    data = json.loads(line)
                    if set(data) == {"delta"} and isinstance(data["delta"], str):
                        yield ("delta", data["delta"])
                    elif set(data) == {"metering"}:
                        usage = Usage(
                            input_tokens=data["metering"]["in"],
                            output_tokens=data["metering"]["out"],
                        )
                        if (
                            usage.input_tokens > len(request.prompt.encode())
                            or usage.output_tokens > request.max_output_tokens
                        ):
                            raise ValueError("stream usage exceeds reservation")
                        yield ("usage", usage)
                    else:
                        raise ValueError("invalid stream event")
            if pending:
                raise ValueError("incomplete NDJSON frame")
