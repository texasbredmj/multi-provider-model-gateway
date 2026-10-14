from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Generation(Strict):
    model: str = Field(pattern=r"^[a-z-]{1,40}$")
    prompt: str = Field(min_length=1, max_length=2048)
    mode: Literal["text", "json", "tools"] = "text"
    stream: bool = False
    tools: list[str] = Field(default_factory=list, max_length=4)
    max_output_tokens: int = Field(default=32, ge=8, le=256)
    fallback_models: list[str] = Field(default_factory=list, max_length=1)
    max_cost_nusd: int = Field(default=1_000_000, ge=1, le=10_000_000)

    @model_validator(mode="after")
    def coherent(self):
        if (
            len(set(self.fallback_models)) != len(self.fallback_models)
            or self.model in self.fallback_models
        ):
            raise ValueError("fallback models must be distinct")
        if self.stream and self.mode != "text":
            raise ValueError("streaming currently supports text only")
        if self.mode == "tools" and self.tools != ["lookup_stock"]:
            raise ValueError("tool mode requires the declared lookup_stock tool")
        if self.mode != "tools" and self.tools:
            raise ValueError("tool declarations require tool mode")
        if not self.prompt.strip():
            raise ValueError("empty prompt")
        return self


class Usage(Strict):
    input_tokens: int = Field(ge=0, le=8192)
    output_tokens: int = Field(ge=0, le=256)


class ToolCall(Strict):
    name: Literal["lookup_stock"]
    arguments: dict[Literal["sku"], Literal["demo-kit"]]

    @model_validator(mode="after")
    def complete(self):
        if self.arguments != {"sku": "demo-kit"}:
            raise ValueError("invalid tool arguments")
        return self


class Structured(Strict):
    answer: str = Field(max_length=128)
    verified: bool


@dataclass(frozen=True)
class Model:
    provider: str
    route: str
    modes: tuple[str, ...]
    streaming: bool
    context_tokens: int
    input_nusd_per_token: int
    output_nusd_per_token: int


MODELS = {
    "alpha-text": Model("alpha", "normal", ("text",), False, 1024, 1000, 2000),
    "alpha-pro": Model("alpha", "normal", ("text", "json", "tools"), False, 4096, 1000, 2000),
    "alpha-rate": Model("alpha", "rate", ("text",), False, 1024, 1000, 2000),
    "alpha-unavailable": Model("alpha", "unavailable", ("text",), False, 1024, 1000, 2000),
    "alpha-timeout": Model("alpha", "timeout", ("text",), False, 1024, 1000, 2000),
    "beta-text": Model("beta", "normal", ("text",), True, 8192, 1500, 2500),
    "beta-invalid": Model("beta", "invalid", ("text",), False, 8192, 1500, 2500),
    "beta-stream-cut": Model("beta", "cut", ("text",), True, 8192, 1500, 2500),
}


def select(request: Generation) -> list[tuple[str, Model]]:
    candidates = []
    input_bound = len(request.prompt.encode("utf-8"))
    for name in [request.model, *request.fallback_models]:
        model = MODELS.get(name)
        if model is None:
            raise ValueError("unknown configured model")
        if request.mode not in model.modes or (request.stream and not model.streaming):
            raise ValueError("requested capability is unsupported by a selected model")
        if input_bound + request.max_output_tokens > model.context_tokens:
            raise ValueError("context bound exceeds selected model capacity")
        candidates.append((name, model))
    return candidates
