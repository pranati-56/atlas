"""The facade every caller uses.

Picks the provider from the model id, retries transient failures, and reports
usage to whoever is accounting for it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from atlas.config import settings
from atlas.llm.providers import PROVIDERS
from atlas.llm.registry import provider_for, spec_for
from atlas.llm.types import (
    ZERO_USAGE,
    ChatRequest,
    ChatResult,
    EmbedKind,
    EmbedRequest,
    ProviderError,
    Purpose,
    StreamEnd,
    Usage,
    UsageEvent,
)

log = logging.getLogger("atlas.llm")

MAX_ATTEMPTS = 4
#: Upper bound per embed request; larger inputs are split automatically.
EMBED_BATCH = 100

# ───────────────────────────────────────────────────────────────── accounting ──

UsageSink = Callable[[UsageEvent], None]

_sink: UsageSink | None = None


def set_usage_sink(fn: UsageSink | None) -> None:
    """Registered once by the observability layer.

    A module-level hook rather than a parameter so no call site has to thread a
    recorder through — an unattributed LLM call is the normal way cost tracking
    quietly becomes wrong.
    """
    global _sink
    _sink = fn


def _record(
    model: str, purpose: Purpose, usage: Usage, error: str | None = None
) -> None:
    if _sink is None:
        return
    try:
        _sink(
            UsageEvent(
                provider=provider_for(model),
                model=model,
                purpose=purpose,
                usage=usage,
                error=error,
            )
        )
    except Exception:  # noqa: BLE001 — accounting must never break the request
        log.exception("usage sink raised")


# ─────────────────────────────────────────────────────────────────── retrying ──


def _status_of(err: BaseException) -> int | None:
    if isinstance(err, ProviderError):
        return err.status
    for attr in ("status_code", "status", "code"):
        value = getattr(err, attr, None)
        if isinstance(value, int):
            return value
    found = re.search(r"\b(4\d{2}|5\d{2})\b", str(err))
    return int(found.group(1)) if found else None


def _retryable(err: BaseException) -> bool:
    """429 and 5xx are worth another try; 400/401/403 are not."""
    if isinstance(err, ProviderError):
        return err.retryable
    status = _status_of(err)
    if status is None:
        return True  # network-level failure
    return status == 429 or status >= 500


async def _with_retry(fn: Callable[[], Any], label: str) -> Any:
    last: BaseException | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — classified by _retryable
            last = err
            if not _retryable(err) or attempt == MAX_ATTEMPTS - 1:
                break
            # Exponential with jitter, so a burst of parallel batches does not
            # all come back at the same instant and rate-limit each other again.
            await asyncio.sleep(0.4 * 2**attempt + random.random() * 0.25)
    raise RuntimeError(f"{label}: {last}") from last


# ───────────────────────────────────────────────────────────────── embeddings ──


async def _embed_chunked(
    model: str,
    texts: list[str],
    kind: EmbedKind,
    dim: int,
    acc: list[Usage],
) -> list[list[float]]:
    """Embeds a batch, halving on failure.

    Splitting rather than failing covers both ways a batch can be rejected — too
    many inputs, or too many tokens across them — without having to know which
    happened, and degrades to one-at-a-time in the worst case.
    """
    if not texts:
        return []

    provider = PROVIDERS[provider_for(model)]
    try:
        res = await _with_retry(
            lambda: provider.embed(EmbedRequest(model, texts, kind, dim)), "embed"
        )
        acc.append(res.usage)
        return list(res.vectors)
    except asyncio.CancelledError:
        raise
    except Exception:
        if len(texts) == 1:
            raise
        mid = (len(texts) + 1) // 2
        left, right = await asyncio.gather(
            _embed_chunked(model, texts[:mid], kind, dim, acc),
            _embed_chunked(model, texts[mid:], kind, dim, acc),
        )
        return [*left, *right]


async def embed(
    texts: Sequence[str],
    kind: EmbedKind,
    *,
    model: str | None = None,
    dim: int | None = None,
    on_progress: Callable[[int, int], Any] | None = None,
) -> tuple[list[list[float]], Usage]:
    cfg = settings()
    model = model or cfg.embedding_model
    dim = dim or cfg.embedding_dim
    acc: list[Usage] = []
    out: list[list[float]] = []
    items = list(texts)

    try:
        for i in range(0, len(items), EMBED_BATCH):
            batch = items[i : i + EMBED_BATCH]
            out.extend(await _embed_chunked(model, batch, kind, dim, acc))
            if on_progress is not None:
                result = on_progress(len(out), len(items))
                if asyncio.iscoroutine(result):
                    await result
    finally:
        _record(model, "embed", sum(acc, ZERO_USAGE))

    return out, sum(acc, ZERO_USAGE)


async def embed_one(
    text: str,
    kind: EmbedKind,
    *,
    model: str | None = None,
    dim: int | None = None,
) -> tuple[list[float], Usage]:
    vectors, usage = await embed([text], kind, model=model, dim=dim)
    if not vectors:
        raise RuntimeError("embed returned no vector")
    return vectors[0], usage


# ───────────────────────────────────────────────────────────────── generation ──


async def complete(
    *,
    prompt: str,
    purpose: Purpose,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
    json_schema: dict[str, Any] | None = None,
) -> ChatResult:
    model = model or settings().generation_model
    provider = PROVIDERS[provider_for(model)]
    req = ChatRequest(
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
        system=system,
        temperature=temperature,
        json_schema=json_schema,
    )
    try:
        res: ChatResult = await _with_retry(lambda: provider.complete(req), purpose)
        _record(model, purpose, res.usage)
        return res
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 — recorded, then re-raised
        _record(model, purpose, ZERO_USAGE, str(err))
        raise


def parse_json(text: str) -> Any | None:
    """First JSON object or array in the text, or None. Tolerates code fences."""
    trimmed = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    match = re.search(r"[\[{][\s\S]*[\]}]", trimmed)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


async def complete_json(
    *,
    prompt: str,
    purpose: Purpose,
    schema: dict[str, Any],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
) -> tuple[Any | None, str, Usage]:
    """Structured generation.

    Prefers native schema-constrained decoding where the model supports it, and
    falls back to extracting the first JSON value from the text otherwise. The
    fallback matters: a model without constrained decoding still usually answers
    correctly, just wrapped in a code fence or a sentence of preamble.
    """
    model = model or settings().generation_model
    native = spec_for(model).supports_json_schema

    res = await complete(
        prompt=(
            prompt
            if native
            else f"{prompt}\n\nReply with JSON matching this schema and nothing "
            f"else:\n{json.dumps(schema)}"
        ),
        purpose=purpose,
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        json_schema=schema if native else None,
    )
    return parse_json(res.text), res.text, res.usage


async def stream(
    *,
    prompt: str,
    purpose: Purpose,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
) -> AsyncIterator[str | StreamEnd]:
    """Yields text deltas, then one StreamEnd.

    Not retried: tokens already delivered to the client cannot be un-sent, so a
    mid-stream failure surfaces rather than silently restarting the answer.
    """
    model = model or settings().generation_model
    provider = PROVIDERS[provider_for(model)]
    req = ChatRequest(
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
        system=system,
        temperature=temperature,
    )

    try:
        async for item in provider.stream(req):
            if isinstance(item, StreamEnd):
                _record(model, purpose, item.usage)
            yield item
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001 — recorded, then re-raised
        _record(model, purpose, ZERO_USAGE, str(err))
        raise
