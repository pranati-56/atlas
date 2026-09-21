"""Evaluation: datasets, runs, metrics, comparison.

Every retrieval knob Atlas exposes is a hypothesis. This is the part that
decides whether any of them helped.
"""

from atlas.eval.compare import Comparison, compare
from atlas.eval.metrics import Accumulator, Retrieved, Truth
from atlas.eval.runner import RunConfig, RunReport, run_dataset

__all__ = [
    "Accumulator",
    "Comparison",
    "RunConfig",
    "RunReport",
    "Retrieved",
    "Truth",
    "compare",
    "run_dataset",
]
