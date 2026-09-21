"""Retrieval and answer metrics.

Pure functions over ranked lists. No database, no network, no model — which is
what makes them testable, and a metric nobody can test is a metric nobody should
trust.

Two things here are easy to get quietly wrong, so both are explicit.

**Ranks are 1-based and order is load-bearing.** Recall@k, MRR and nDCG are all
undefined over an unordered set. Everything below takes the retrieved list in
the order the retriever produced it.

**Document-level truth is scored over distinct documents.** Retrieval returns
chunks, and eight chunks from one document are one document. Scoring them as
eight would let a retriever that found a single document and nothing else report
precision@8 = 1.0. The list is deduplicated by first occurrence — which is also
the rank that matters, since that is where a reader would find it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

#: What the ground truth is expressed in. Chunk-level is strictest; source-level
#: only asks "did it look in the right place", which is still worth knowing when
#: labelling chunks by hand is not realistic.
Level = str  # "chunk" | "document" | "source"


@dataclass(frozen=True, slots=True)
class Truth:
    """Labelled ground truth for one question."""

    chunk_ids: frozenset[str] = frozenset()
    document_ids: frozenset[str] = frozenset()
    sources: frozenset[str] = frozenset()

    @property
    def level(self) -> Level | None:
        """The strictest level this question is actually labelled at."""
        if self.chunk_ids:
            return "chunk"
        if self.document_ids:
            return "document"
        if self.sources:
            return "source"
        return None

    def relevant(self) -> frozenset[str]:
        return {
            "chunk": self.chunk_ids,
            "document": self.document_ids,
            "source": self.sources,
        }.get(self.level or "", frozenset())


@dataclass(frozen=True, slots=True)
class Retrieved:
    """One retrieved chunk, reduced to the three ids a metric can match on."""

    chunk_id: str
    document_id: str
    source_kind: str

    def key(self, level: Level) -> str:
        return {
            "chunk": self.chunk_id,
            "document": self.document_id,
            "source": self.source_kind,
        }[level]


def ranked_keys(retrieved: Sequence[Retrieved], level: Level) -> list[str]:
    """The retrieved list as keys at `level`, deduplicated by first occurrence.

    First occurrence rather than best score: they are the same thing here, since
    the list arrives already ordered, and it keeps the rank meaningful.
    """
    seen: set[str] = set()
    out: list[str] = []
    for r in retrieved:
        key = r.key(level)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ─────────────────────────────────────────────────────────────── retrieval ──


def recall_at_k(keys: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """Share of the labelled set that appears in the top k."""
    if not relevant:
        return float("nan")
    return sum(1 for key in keys[:k] if key in relevant) / len(relevant)


def precision_at_k(keys: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """Share of the top k that is labelled relevant.

    The denominator is min(k, len(keys)), not k: when only three rows came back
    at all, scoring them out of eight measures the corpus, not the ranker.
    """
    if not relevant:
        return float("nan")
    window = keys[:k]
    if not window:
        return 0.0
    return sum(1 for key in window if key in relevant) / len(window)


def first_relevant_rank(keys: Sequence[str], relevant: frozenset[str]) -> int | None:
    for i, key in enumerate(keys, start=1):
        if key in relevant:
            return i
    return None


def reciprocal_rank(keys: Sequence[str], relevant: frozenset[str]) -> float:
    """1/rank of the first relevant row. Zero when none was retrieved."""
    if not relevant:
        return float("nan")
    rank = first_relevant_rank(keys, relevant)
    return 1.0 / rank if rank else 0.0


def ndcg_at_k(keys: Sequence[str], relevant: frozenset[str], k: int) -> float:
    """Binary-relevance nDCG.

    Binary because the labels are binary: a passage either answers the question
    or it does not. Graded relevance needs graded labels, and inventing grades to
    feed a formula is how a metric stops meaning anything.
    """
    if not relevant:
        return float("nan")
    dcg = sum(
        1.0 / math.log2(i + 1)
        for i, key in enumerate(keys[:k], start=1)
        if key in relevant
    )
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def ranks_of(keys: Sequence[str], relevant: frozenset[str]) -> dict[str, int | None]:
    """Where each labelled id landed, or null if it never came back.

    This is the column to read when a metric drops: it distinguishes "the right
    passage fell from rank 2 to rank 9" from "it vanished entirely", and those
    have completely different causes.
    """
    position = {key: i for i, key in enumerate(keys, start=1)}
    return {key: position.get(key) for key in sorted(relevant)}


# ───────────────────────────────────────────────────────────────── answers ──


def includes(answer: str, required: Sequence[str]) -> tuple[bool, list[str]]:
    """Case-insensitive substring assertions. Returns (ok, the ones missing)."""
    haystack = answer.lower()
    missing = [s for s in required if s.lower() not in haystack]
    return (not missing, missing)


def excludes(answer: str, forbidden: Sequence[str]) -> tuple[bool, list[str]]:
    """Returns (ok, the ones that appeared and should not have)."""
    haystack = answer.lower()
    present = [s for s in forbidden if s.lower() in haystack]
    return (not present, present)


def citation_accuracy(
    cited_chunk_ids: Sequence[str],
    retrieved_chunk_ids: Sequence[str],
    relevant_chunk_ids: frozenset[str] = frozenset(),
) -> float:
    """Share of the answer's citations that point somewhere real.

    "Real" means the passage was actually retrieved for this question — a marker
    pointing at nothing is the failure mode worth catching, because to a reader
    it looks exactly like a genuine one.

    When chunk-level ground truth exists the bar rises: the citation must point
    at a passage labelled relevant, not merely at one that happened to be in the
    context window.
    """
    if not cited_chunk_ids:
        # No citations at all. Not wrong on its own — an abstention has none —
        # so this reads as absent rather than as zero, and the aggregate skips
        # it instead of averaging in a penalty.
        return float("nan")
    target = relevant_chunk_ids or frozenset(retrieved_chunk_ids)
    return sum(1 for c in cited_chunk_ids if c in target) / len(cited_chunk_ids)


#: Phrasings that mean "I could not find this". Used only to score questions
#: labelled unanswerable, where abstaining is correct and a confident answer is
#: a hallucination.
_ABSTAIN_MARKERS = (
    "nothing in the corpus",
    "could not find",
    "couldn't find",
    "i don't have",
    "i do not have",
    "not in these documents",
    "no passage",
    "does not appear in",
    "is not covered",
    "not grounded in",
)


def abstained(answer: str) -> bool:
    low = answer.lower()
    return any(marker in low for marker in _ABSTAIN_MARKERS)


# ─────────────────────────────────────────────────────────────── aggregate ──


def _mean(values: Sequence[float]) -> float | None:
    """Mean over the values that exist.

    NaN means "not applicable to this question" — an unlabelled question has no
    recall — and averaging it in as zero would punish a run for the state of its
    dataset rather than the state of its retriever.
    """
    real = [v for v in values if v == v]  # NaN != NaN
    return sum(real) / len(real) if real else None


def _percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank. With the tens of questions a hand-labelled set realistically
    # has, interpolating between two samples implies a precision that is not
    # there.
    index = min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))
    return ordered[index]


@dataclass
class Accumulator:
    """Collects per-question results and produces the run-level metrics blob.

    Key names are fixed by `eval_run_summary` in 004_eval.sql — the view reads
    them out of the jsonb by name, so renaming one here silently empties a
    column there.
    """

    recall: list[float] = field(default_factory=list)
    precision: list[float] = field(default_factory=list)
    rr: list[float] = field(default_factory=list)
    ndcg: list[float] = field(default_factory=list)
    citation: list[float] = field(default_factory=list)
    faithfulness: list[float] = field(default_factory=list)
    latency_ms: list[float] = field(default_factory=list)
    cost_usd: list[float] = field(default_factory=list)

    hits: int = 0
    scored: int = 0
    include_pass: int = 0
    include_total: int = 0
    exclude_pass: int = 0
    exclude_total: int = 0
    abstain_pass: int = 0
    abstain_total: int = 0
    errors: int = 0

    def add(self, per_question: dict[str, Any]) -> None:
        for key, sink in (
            ("recall_at_k", self.recall),
            ("precision_at_k", self.precision),
            ("reciprocal_rank", self.rr),
            ("ndcg", self.ndcg),
            ("citation_accuracy", self.citation),
            ("faithfulness", self.faithfulness),
        ):
            value = per_question.get(key)
            if value is not None:
                sink.append(float(value))

        if per_question.get("latency_ms") is not None:
            self.latency_ms.append(float(per_question["latency_ms"]))
        if per_question.get("cost_usd") is not None:
            self.cost_usd.append(float(per_question["cost_usd"]))

        if per_question.get("error"):
            self.errors += 1

        if per_question.get("labelled"):
            self.scored += 1
            if per_question.get("hit"):
                self.hits += 1

        for flag, passed, total in (
            ("includes_ok", "include_pass", "include_total"),
            ("excludes_ok", "exclude_pass", "exclude_total"),
            ("abstention_ok", "abstain_pass", "abstain_total"),
        ):
            value = per_question.get(flag)
            if value is not None:
                setattr(self, total, getattr(self, total) + 1)
                if value:
                    setattr(self, passed, getattr(self, passed) + 1)

    def summary(self) -> dict[str, Any]:
        def ratio(passed: int, total: int) -> float | None:
            return passed / total if total else None

        total_cost = sum(self.cost_usd)
        return {
            "recall_at_k": _mean(self.recall),
            "precision_at_k": _mean(self.precision),
            "mrr": _mean(self.rr),
            "ndcg": _mean(self.ndcg),
            "hit_rate": ratio(self.hits, self.scored),
            "citation_accuracy": _mean(self.citation),
            "faithfulness": _mean(self.faithfulness),
            "must_include_pass_rate": ratio(self.include_pass, self.include_total),
            "must_not_include_pass_rate": ratio(self.exclude_pass, self.exclude_total),
            "abstention_accuracy": ratio(self.abstain_pass, self.abstain_total),
            "p50_ms": median(self.latency_ms) if self.latency_ms else None,
            "p95_ms": _percentile(self.latency_ms, 0.95),
            "total_cost_usd": round(total_cost, 6) if self.cost_usd else 0.0,
            "cost_per_query": (
                round(total_cost / len(self.cost_usd), 6) if self.cost_usd else 0.0
            ),
            "questions_scored": self.scored,
            "errors": self.errors,
        }
