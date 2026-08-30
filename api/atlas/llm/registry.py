"""Model catalog: which provider serves a model, what it costs, what it refuses.

`supports_sampling` is load-bearing, not cosmetic. Anthropic removed
temperature/top_p/top_k on Opus 4.7 and later — sending any of them returns a
400 rather than being ignored. A provider-agnostic caller that always passes a
temperature would break on exactly the newest models, so the adapter reads this
flag and drops the parameter.

Prices are USD per million tokens. Anthropic figures verified against published
rates 2026-06-24; the Gemini and OpenAI figures are best-effort and should be
re-checked against each provider's pricing page before anyone quotes a
cost-per-query number externally. Override any of them with no code change via
ATLAS_PRICE_OVERRIDES (JSON: {"model-id": {"in": 1.23, "out": 4.56}}).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache

from atlas.config import settings
from atlas.llm.types import ProviderId

log = logging.getLogger("atlas.llm.registry")


@dataclass(frozen=True, slots=True)
class ModelSpec:
    provider: ProviderId
    input_per_1m: float
    output_per_1m: float
    max_output: int
    context_window: int
    #: False -> the adapter must not send temperature/top_p/top_k.
    supports_sampling: bool
    #: Native constrained decoding against a JSON Schema.
    supports_json_schema: bool
    embedding: bool = False


MODELS: dict[str, ModelSpec] = {
    # ── anthropic ────────────────────────────────────────────────────────
    # Sampling parameters return a 400 across this family (Haiku 4.5 excepted).
    "claude-opus-5": ModelSpec("anthropic", 5.0, 25.0, 128_000, 1_000_000, False, True),
    "claude-sonnet-5": ModelSpec("anthropic", 3.0, 15.0, 128_000, 1_000_000, False, True),
    "claude-opus-4-8": ModelSpec("anthropic", 5.0, 25.0, 128_000, 1_000_000, False, True),
    "claude-haiku-4-5": ModelSpec("anthropic", 1.0, 5.0, 64_000, 200_000, True, True),
    # ── gemini ───────────────────────────────────────────────────────────
    "gemini-2.5-flash": ModelSpec("gemini", 0.30, 2.50, 65_536, 1_048_576, True, True),
    "gemini-2.5-pro": ModelSpec("gemini", 1.25, 10.0, 65_536, 1_048_576, True, True),
    "gemini-2.0-flash": ModelSpec("gemini", 0.10, 0.40, 8_192, 1_048_576, True, True),
    "gemini-embedding-001": ModelSpec(
        "gemini", 0.15, 0.0, 0, 2_048, False, False, embedding=True
    ),
    # ── openai ───────────────────────────────────────────────────────────
    "gpt-4.1-mini": ModelSpec("openai", 0.40, 1.60, 32_768, 1_047_576, True, True),
    "text-embedding-3-small": ModelSpec(
        "openai", 0.02, 0.0, 0, 8_191, False, False, embedding=True
    ),
}


@lru_cache(maxsize=1)
def _price_overrides() -> dict[str, dict[str, float]]:
    raw = settings().price_overrides
    if not raw:
        return {}
    try:
        parsed: dict[str, dict[str, float]] = json.loads(raw)
        return parsed
    except (ValueError, TypeError):
        log.warning("ATLAS_PRICE_OVERRIDES is not valid JSON — ignoring")
        return {}


def spec_for(model: str) -> ModelSpec:
    spec = MODELS.get(model)
    if spec is None:
        raise KeyError(
            f'Unknown model "{model}". Add it to atlas/llm/registry.py — an '
            f"unpriced model would silently report a cost of zero."
        )
    return spec


def provider_for(model: str) -> ProviderId:
    return spec_for(model).provider


def cost_of(model: str, input_tokens: int, output_tokens: int, cached: int = 0) -> float:
    """Cached input tokens bill at 10% of the input rate across all three
    providers — close enough to exact for a budget check."""
    spec = spec_for(model)
    override = _price_overrides().get(model, {})
    in_rate = override.get("in", spec.input_per_1m)
    out_rate = override.get("out", spec.output_per_1m)
    billable = max(0, input_tokens - cached)
    return (
        billable * in_rate / 1e6
        + cached * in_rate * 0.1 / 1e6
        + output_tokens * out_rate / 1e6
    )


def estimate_tokens(text: str) -> int:
    """~4 characters per token holds well enough for English prose to size batches."""
    return max(1, -(-len(text) // 4))


GENERATION_MODELS = [m for m, s in MODELS.items() if not s.embedding]
EMBEDDING_MODELS = [m for m, s in MODELS.items() if s.embedding]
