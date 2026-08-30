"""The three provider adapters.

Each one normalises its SDK's shape into the contracts in `types.py`: unit-length
vectors, a `Usage` with a computed cost, and a `StopReason` drawn from the
vendor's own finish-reason vocabulary.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator
from typing import Any

from atlas.config import settings
from atlas.llm.registry import cost_of, spec_for
from atlas.llm.types import (
    ChatRequest,
    ChatResult,
    EmbedRequest,
    EmbedResult,
    ProviderError,
    StopReason,
    StreamEnd,
    Usage,
)


def _l2normalise(v: list[float]) -> list[float]:
    """Truncated Matryoshka outputs are not unit-length; cosine needs them to be."""
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 0 else v


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _rough_tokens(text: str) -> int:
    return max(1, -(-len(text) // 4))


# ────────────────────────────────────────────────────────────────── gemini ──


class GeminiProvider:
    id = "gemini"

    def __init__(self) -> None:
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is not None:
            return self._client
        key = settings().gemini_api_key
        if not key:
            raise ProviderError("gemini", "GEMINI_API_KEY is not set", retryable=False)
        from google import genai

        self._client = genai.Client(api_key=key)
        return self._client

    @staticmethod
    def _thinking(model: str) -> dict[str, Any]:
        """2.5-series models think before answering, and thinking tokens come out
        of max_output_tokens. On a grounded extractive task that spends the
        budget for no benefit, and at low limits it can consume the whole
        allowance and return an empty candidate. Pro cannot disable it."""
        if not model.startswith("gemini-2.5-flash"):
            return {}
        from google.genai import types as gt

        return {"thinking_config": gt.ThinkingConfig(thinking_budget=0)}

    @staticmethod
    def _stop(reason: Any) -> StopReason:
        name = getattr(reason, "name", None) or (str(reason) if reason else "")
        if name in ("", "STOP", "FINISH_REASON_UNSPECIFIED"):
            return "stop"
        if name == "MAX_TOKENS":
            return "max_tokens"
        if name in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            return "safety"
        return "other"

    async def embed(self, req: EmbedRequest) -> EmbedResult:
        from google.genai import types as gt

        started = time.monotonic()
        res = await self._sdk().aio.models.embed_content(
            model=req.model,
            contents=req.texts,
            config=gt.EmbedContentConfig(
                task_type=(
                    "RETRIEVAL_QUERY" if req.kind == "query" else "RETRIEVAL_DOCUMENT"
                ),
                output_dimensionality=req.dim,
            ),
        )

        out = list(res.embeddings or [])
        if len(out) != len(req.texts):
            raise ProviderError(
                "gemini", f"expected {len(req.texts)} embeddings, received {len(out)}"
            )

        vectors: list[list[float]] = []
        for i, e in enumerate(out):
            values = list(e.values or [])
            if not values:
                raise ProviderError("gemini", f"empty embedding at index {i}")
            if len(values) != req.dim:
                raise ProviderError(
                    "gemini",
                    f"embedding is {len(values)}-dimensional but "
                    f"ATLAS_EMBEDDING_DIM is {req.dim}; the vector(N) column "
                    f"must match",
                    retryable=False,
                )
            vectors.append(_l2normalise(values))

        # The embed endpoint does not report token usage.
        tokens = sum(_rough_tokens(t) for t in req.texts)
        return EmbedResult(
            vectors=vectors,
            usage=Usage(
                input_tokens=tokens,
                cost_usd=cost_of(req.model, tokens, 0),
                ms=_elapsed_ms(started),
            ),
        )

    def _config(self, req: ChatRequest) -> Any:
        from google.genai import types as gt

        spec = spec_for(req.model)
        kwargs: dict[str, Any] = {
            "system_instruction": req.system,
            "max_output_tokens": min(req.max_tokens, spec.max_output),
            **self._thinking(req.model),
        }
        if spec.supports_sampling and req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.json_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = req.json_schema
        return gt.GenerateContentConfig(**kwargs)

    async def complete(self, req: ChatRequest) -> ChatResult:
        started = time.monotonic()
        res = await self._sdk().aio.models.generate_content(
            model=req.model, contents=req.prompt, config=self._config(req)
        )
        u = res.usage_metadata
        input_tokens = getattr(u, "prompt_token_count", 0) or 0
        output_tokens = getattr(u, "candidates_token_count", 0) or 0
        cached = getattr(u, "cached_content_token_count", 0) or 0
        finish = res.candidates[0].finish_reason if res.candidates else None

        return ChatResult(
            text=(res.text or "").strip(),
            stop_reason=self._stop(finish),
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                cost_usd=cost_of(req.model, input_tokens, output_tokens, cached),
                ms=_elapsed_ms(started),
            ),
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | StreamEnd]:
        started = time.monotonic()
        stop: StopReason = "stop"
        input_tokens = output_tokens = cached = 0

        stream = await self._sdk().aio.models.generate_content_stream(
            model=req.model, contents=req.prompt, config=self._config(req)
        )
        async for chunk in stream:
            if chunk.candidates and chunk.candidates[0].finish_reason:
                stop = self._stop(chunk.candidates[0].finish_reason)
            # Every chunk carries cumulative usage; the last one wins.
            if chunk.usage_metadata:
                u = chunk.usage_metadata
                input_tokens = getattr(u, "prompt_token_count", 0) or input_tokens
                output_tokens = getattr(u, "candidates_token_count", 0) or output_tokens
                cached = getattr(u, "cached_content_token_count", 0) or cached
            if chunk.text:
                yield chunk.text

        yield StreamEnd(
            stop_reason=stop,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                cost_usd=cost_of(req.model, input_tokens, output_tokens, cached),
                ms=_elapsed_ms(started),
            ),
        )


# ────────────────────────────────────────────────────────────────── openai ──


class OpenAIProvider:
    id = "openai"

    def __init__(self) -> None:
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is not None:
            return self._client
        key = settings().openai_api_key
        if not key:
            raise ProviderError("openai", "OPENAI_API_KEY is not set", retryable=False)
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=key)
        return self._client

    @staticmethod
    def _stop(reason: str | None) -> StopReason:
        if reason in (None, "stop"):
            return "stop"
        if reason == "length":
            return "max_tokens"
        if reason == "content_filter":
            return "safety"
        return "other"

    async def embed(self, req: EmbedRequest) -> EmbedResult:
        started = time.monotonic()
        res = await self._sdk().embeddings.create(
            model=req.model, input=req.texts, dimensions=req.dim
        )
        # Response order is not promised; the index field is.
        ordered = sorted(res.data, key=lambda d: d.index)
        input_tokens = res.usage.prompt_tokens if res.usage else 0
        return EmbedResult(
            vectors=[_l2normalise(list(d.embedding)) for d in ordered],
            usage=Usage(
                input_tokens=input_tokens,
                cost_usd=cost_of(req.model, input_tokens, 0),
                ms=_elapsed_ms(started),
            ),
        )

    def _kwargs(self, req: ChatRequest) -> dict[str, Any]:
        spec = spec_for(req.model)
        messages: list[dict[str, str]] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.append({"role": "user", "content": req.prompt})

        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": min(req.max_tokens, spec.max_output),
            "messages": messages,
        }
        if spec.supports_sampling and req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if req.json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "atlas_response",
                    "schema": req.json_schema,
                    "strict": True,
                },
            }
        return kwargs

    async def complete(self, req: ChatRequest) -> ChatResult:
        started = time.monotonic()
        res = await self._sdk().chat.completions.create(**self._kwargs(req))
        choice = res.choices[0] if res.choices else None
        u = res.usage
        input_tokens = u.prompt_tokens if u else 0
        output_tokens = u.completion_tokens if u else 0
        details = getattr(u, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0

        return ChatResult(
            text=((choice.message.content if choice else None) or "").strip(),
            stop_reason=self._stop(choice.finish_reason if choice else None),
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                cost_usd=cost_of(req.model, input_tokens, output_tokens, cached),
                ms=_elapsed_ms(started),
            ),
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | StreamEnd]:
        started = time.monotonic()
        kwargs = self._kwargs(req)
        kwargs.pop("response_format", None)
        kwargs["stream"] = True
        # Usage is omitted from streamed responses unless asked for explicitly.
        kwargs["stream_options"] = {"include_usage": True}

        stop: StopReason = "stop"
        input_tokens = output_tokens = cached = 0

        stream = await self._sdk().chat.completions.create(**kwargs)
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if choice and choice.finish_reason:
                stop = self._stop(choice.finish_reason)
            if chunk.usage:
                input_tokens = chunk.usage.prompt_tokens or input_tokens
                output_tokens = chunk.usage.completion_tokens or output_tokens
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", 0) or cached
            delta = choice.delta.content if choice and choice.delta else None
            if delta:
                yield delta

        yield StreamEnd(
            stop_reason=stop,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                cost_usd=cost_of(req.model, input_tokens, output_tokens, cached),
                ms=_elapsed_ms(started),
            ),
        )


# ─────────────────────────────────────────────────────────────── anthropic ──


class AnthropicProvider:
    """Two things this adapter has to get right that the others do not:

    1. Sampling parameters were removed on Opus 4.7 and later — sending a
       temperature returns a 400 rather than being ignored. `supports_sampling`
       in the registry gates it.
    2. `stop_reason == "refusal"` arrives as a successful HTTP 200 with empty or
       partial content. Code that reads content[0] unconditionally breaks on it,
       so the stop reason is checked before the content is touched.
    """

    id = "anthropic"

    def __init__(self) -> None:
        self._client: Any = None

    def _sdk(self) -> Any:
        if self._client is not None:
            return self._client
        key = settings().anthropic_api_key
        if not key:
            raise ProviderError(
                "anthropic", "ANTHROPIC_API_KEY is not set", retryable=False
            )
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=key)
        return self._client

    @staticmethod
    def _stop(reason: str | None) -> StopReason:
        if reason in (None, "end_turn", "stop_sequence"):
            return "stop"
        if reason == "max_tokens":
            return "max_tokens"
        if reason == "refusal":
            return "refusal"
        return "other"

    async def embed(self, req: EmbedRequest) -> EmbedResult:
        raise ProviderError(
            "anthropic",
            "Anthropic has no embeddings endpoint — set ATLAS_EMBEDDING_MODEL "
            "to a Gemini or OpenAI embedding model.",
            retryable=False,
        )

    def _kwargs(self, req: ChatRequest) -> dict[str, Any]:
        spec = spec_for(req.model)
        kwargs: dict[str, Any] = {
            "model": req.model,
            "max_tokens": min(req.max_tokens, spec.max_output),
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if req.system:
            kwargs["system"] = req.system
        if spec.supports_sampling and req.temperature is not None:
            kwargs["temperature"] = req.temperature
        return kwargs

    async def complete(self, req: ChatRequest) -> ChatResult:
        started = time.monotonic()
        kwargs = self._kwargs(req)
        if req.json_schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": req.json_schema}
            }

        res = await self._sdk().messages.create(**kwargs)
        stop = self._stop(res.stop_reason)
        # Guarded: on a refusal `content` is empty and indexing it would raise.
        text = (
            ""
            if stop == "refusal"
            else "".join(b.text for b in res.content if b.type == "text").strip()
        )

        input_tokens = res.usage.input_tokens
        output_tokens = res.usage.output_tokens
        cached = getattr(res.usage, "cache_read_input_tokens", 0) or 0

        return ChatResult(
            text=text,
            stop_reason=stop,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                cost_usd=cost_of(req.model, input_tokens, output_tokens, cached),
                ms=_elapsed_ms(started),
            ),
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[str | StreamEnd]:
        started = time.monotonic()
        async with self._sdk().messages.stream(**self._kwargs(req)) as s:
            async for text in s.text_stream:
                yield text
            final = await s.get_final_message()

        input_tokens = final.usage.input_tokens
        output_tokens = final.usage.output_tokens
        cached = getattr(final.usage, "cache_read_input_tokens", 0) or 0

        yield StreamEnd(
            stop_reason=self._stop(final.stop_reason),
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached,
                cost_usd=cost_of(req.model, input_tokens, output_tokens, cached),
                ms=_elapsed_ms(started),
            ),
        )


PROVIDERS: dict[str, Any] = {
    "gemini": GeminiProvider(),
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
}
