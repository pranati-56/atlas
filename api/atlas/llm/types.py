"""Provider-agnostic LLM contracts. Nothing above this layer names a vendor."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, runtime_checkable

ProviderId = Literal["gemini", "openai", "anthropic"]

# What the call was for. Written to llm_calls.purpose for cost attribution.
Purpose = Literal[
    "embed",
    "rewrite",
    "plan",
    "rerank",
    "answer",
    "verify",
    "contextualise",
]

StopReason = Literal["stop", "max_tokens", "refusal", "safety", "error", "other"]

# Corpus chunks embed as "document", the user's question as "query". The same
# text produces different vectors under each, and mixing them degrades recall
# without raising an error anywhere — which is why this is required, not
# defaulted.
EmbedKind = Literal["document", "query"]


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    ms: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            ms=self.ms + other.ms,
        )

    def with_ms(self, ms: int) -> Usage:
        return replace(self, ms=ms)


ZERO_USAGE = Usage()


@dataclass(slots=True)
class EmbedRequest:
    model: str
    texts: list[str]
    kind: EmbedKind
    dim: int


@dataclass(slots=True)
class EmbedResult:
    #: Unit-length. Providers returning unnormalised truncations are fixed here.
    vectors: list[list[float]]
    usage: Usage


@dataclass(slots=True)
class ChatRequest:
    model: str
    prompt: str
    max_tokens: int
    system: str | None = None
    #: Dropped for models that reject sampling parameters — see
    #: ModelSpec.supports_sampling. Dropping rather than erroring is deliberate:
    #: the caller wanted determinism, and those models are near-deterministic by
    #: default, so the intent survives even though the knob does not.
    temperature: float | None = None
    #: When set, the reply is constrained to this JSON Schema.
    json_schema: dict[str, Any] | None = None


@dataclass(slots=True)
class ChatResult:
    text: str
    usage: Usage
    stop_reason: StopReason


@dataclass(slots=True)
class StreamEnd:
    """Terminal value of a streamed completion."""

    usage: Usage
    stop_reason: StopReason


@dataclass(slots=True)
class UsageEvent:
    provider: str
    model: str
    purpose: Purpose
    usage: Usage
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status = status
        # Unknown failures count as retryable: a network blip is far more common
        # than a permanently malformed request that got this far.
        self.retryable = True if retryable is None else retryable


@runtime_checkable
class Provider(Protocol):
    id: ProviderId

    async def embed(self, req: EmbedRequest) -> EmbedResult: ...

    async def complete(self, req: ChatRequest) -> ChatResult: ...

    def stream(self, req: ChatRequest) -> AsyncIterator[str | StreamEnd]:
        """Yields text deltas, then exactly one StreamEnd as the final item.

        A sentinel rather than a generator return value, because an async
        generator's return value is not reachable through `async for`.
        """
        ...
