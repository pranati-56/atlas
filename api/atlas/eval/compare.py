"""Comparing two runs without fooling yourself.

The schema's own header warns about this: change alpha, watch the one question
you happened to be looking at improve, ship a regression across the other
ninety. A mean that moved from 0.71 to 0.74 on forty questions is not evidence
of anything on its own, and printing it next to an arrow is how a team talks
itself into a worse retriever.

So `compare` reports three things instead of one:

  - the paired difference, question by question, because both runs answered the
    *same* questions and a paired comparison is far more sensitive than treating
    them as two independent samples;
  - how many questions improved, regressed and stayed put — which catches the
    case where the mean rose because one question went from 0 to 1 while six
    others quietly slipped;
  - a bootstrap interval, so "this dataset cannot tell them apart yet" is a
    result the tool can actually express.

None of this substitutes for a bigger dataset. It is a way of noticing that you
need one.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: Enough for a stable interval at three decimals, fast enough that `compare`
#: still feels instant on a few hundred questions.
RESAMPLES = 10_000

#: Fixed, so two people comparing the same runs see the same interval. One that
#: shifts on every invocation invites re-rolling until it says what you hoped.
SEED = 20260821


@dataclass(frozen=True, slots=True)
class Paired:
    """One question, scored by both runs."""

    question: str
    before: float
    after: float

    @property
    def delta(self) -> float:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class Comparison:
    metric: str
    n: int
    mean_before: float
    mean_after: float
    delta: float
    improved: int
    regressed: int
    unchanged: int
    ci_low: float
    ci_high: float
    #: Share of bootstrap resamples whose sign disagrees with the observed
    #: difference. Deliberately not called a p-value — it answers "how often
    #: would resampling these same questions have pointed the other way".
    disagreement: float
    biggest_gains: list[Paired]
    biggest_losses: list[Paired]

    @property
    def decisive(self) -> bool:
        """True when the interval clears zero.

        The honest reading of False is "this dataset cannot tell these two
        apart", not "they are the same".
        """
        return self.ci_low > 0 or self.ci_high < 0

    def verdict(self) -> str:
        if self.n == 0:
            return "no questions in common"
        if not self.decisive:
            plural = "s" if self.n != 1 else ""
            return (
                f"inconclusive on {self.n} question{plural} — "
                f"the interval spans zero"
            )
        direction = "better" if self.delta > 0 else "worse"
        return (
            f"{direction} by {abs(self.delta):.3f} "
            f"({self.ci_low:+.3f} to {self.ci_high:+.3f})"
        )


def pair(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    metric: str,
) -> list[Paired]:
    """Questions both runs scored on this metric, keyed by question id.

    Questions only one run answered are dropped rather than filled with zero: a
    question that errored in one run tells you about that error, not about the
    metric, and pairing it against a real score imports the failure into every
    number downstream.
    """
    out: list[Paired] = []
    for qid, b in before.items():
        a = after.get(qid)
        if a is None:
            continue
        bv, av = b.get(metric), a.get(metric)
        if bv is None or av is None:
            continue
        bf, af = float(bv), float(av)
        if bf != bf or af != af:  # NaN — not applicable to this question
            continue
        out.append(Paired(question=str(b.get("question", qid)), before=bf, after=af))
    return out


def bootstrap(pairs: Sequence[Paired], metric: str) -> Comparison:
    """Paired bootstrap over the per-question differences."""
    n = len(pairs)
    if n == 0:
        return Comparison(
            metric=metric,
            n=0,
            mean_before=0.0,
            mean_after=0.0,
            delta=0.0,
            improved=0,
            regressed=0,
            unchanged=0,
            ci_low=0.0,
            ci_high=0.0,
            disagreement=1.0,
            biggest_gains=[],
            biggest_losses=[],
        )

    deltas = [p.delta for p in pairs]
    observed = sum(deltas) / n

    rng = random.Random(SEED)
    means: list[float] = []
    for _ in range(RESAMPLES):
        # Resample questions, not values: the unit of uncertainty here is which
        # questions happened to end up in the dataset.
        total = 0.0
        for _ in range(n):
            total += deltas[rng.randrange(n)]
        means.append(total / n)
    means.sort()

    low = means[int(0.025 * RESAMPLES)]
    high = means[min(RESAMPLES - 1, int(0.975 * RESAMPLES))]

    if observed > 0:
        wrong_sign = sum(1 for m in means if m <= 0)
    elif observed < 0:
        wrong_sign = sum(1 for m in means if m >= 0)
    else:
        wrong_sign = RESAMPLES

    ranked = sorted(pairs, key=lambda p: p.delta)
    return Comparison(
        metric=metric,
        n=n,
        mean_before=sum(p.before for p in pairs) / n,
        mean_after=sum(p.after for p in pairs) / n,
        delta=observed,
        improved=sum(1 for d in deltas if d > 0),
        regressed=sum(1 for d in deltas if d < 0),
        unchanged=sum(1 for d in deltas if d == 0),
        ci_low=low,
        ci_high=high,
        disagreement=wrong_sign / RESAMPLES,
        biggest_losses=[p for p in ranked[:3] if p.delta < 0],
        biggest_gains=[p for p in reversed(ranked[-3:]) if p.delta > 0],
    )


def compare(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    metrics: Sequence[str],
) -> list[Comparison]:
    return [bootstrap(pair(before, after, m), m) for m in metrics]
