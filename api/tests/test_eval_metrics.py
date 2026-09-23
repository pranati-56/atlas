"""The eval harness, in the parts that need no database.

These are the numbers every tuning decision will rest on. A retrieval metric
that is subtly wrong is worse than none at all: it does not stay silent, it
points confidently in a direction, and someone ships that direction.
"""

from __future__ import annotations

import math
from pathlib import Path

from atlas.config import SERVICE_ROOT
from atlas.eval.compare import Paired, bootstrap, pair
from atlas.eval.datasets import load_file, validate
from atlas.eval.metrics import (
    Accumulator,
    Retrieved,
    Truth,
    abstained,
    citation_accuracy,
    excludes,
    includes,
    ndcg_at_k,
    precision_at_k,
    ranked_keys,
    ranks_of,
    recall_at_k,
    reciprocal_rank,
)


def R(chunk: str, doc: str = "d", source: str = "github") -> Retrieved:
    return Retrieved(chunk_id=chunk, document_id=doc, source_kind=source)


# ─────────────────────────────────────────────────────────────────── truth ──


def test_the_strictest_available_label_is_the_one_used() -> None:
    both = Truth(chunk_ids=frozenset({"c1"}), document_ids=frozenset({"d1"}))
    assert both.level == "chunk"
    assert both.relevant() == {"c1"}

    docs_only = Truth(document_ids=frozenset({"d1"}), sources=frozenset({"notion"}))
    assert docs_only.level == "document"

    assert Truth(sources=frozenset({"slack"})).level == "source"
    assert Truth().level is None
    assert Truth().relevant() == frozenset()


def test_document_level_scoring_collapses_chunks_from_one_document() -> None:
    # Eight chunks of one document are one document. Scoring them as eight lets
    # a retriever that found a single document report precision 1.0.
    retrieved = [R("c1", "d1"), R("c2", "d1"), R("c3", "d2"), R("c4", "d1")]
    assert ranked_keys(retrieved, "document") == ["d1", "d2"]
    assert len(ranked_keys(retrieved, "chunk")) == 4


def test_dedup_keeps_the_first_occurrence_because_that_is_the_rank() -> None:
    retrieved = [R("c1", "d2"), R("c2", "d1"), R("c3", "d2")]
    assert ranked_keys(retrieved, "document") == ["d2", "d1"]


# ─────────────────────────────────────────────────────────────── retrieval ──


def test_recall_counts_the_labelled_set_not_the_window() -> None:
    keys = ["a", "b", "c", "d"]
    relevant = frozenset({"a", "c", "z"})  # z was never retrieved
    assert recall_at_k(keys, relevant, 4) == 2 / 3
    # Cutting the window below the relevant row drops it.
    assert recall_at_k(keys, relevant, 1) == 1 / 3


def test_precision_divides_by_what_came_back_not_by_k() -> None:
    # Only two rows exist. Scoring them out of eight measures the corpus.
    assert precision_at_k(["a", "b"], frozenset({"a"}), 8) == 0.5
    assert precision_at_k([], frozenset({"a"}), 8) == 0.0


def test_reciprocal_rank_is_zero_when_nothing_relevant_returns() -> None:
    keys = ["x", "y", "a"]
    assert reciprocal_rank(keys, frozenset({"a"})) == 1 / 3
    assert reciprocal_rank(keys, frozenset({"x"})) == 1.0
    assert reciprocal_rank(keys, frozenset({"nope"})) == 0.0


def test_ndcg_rewards_putting_the_right_row_higher() -> None:
    relevant = frozenset({"a", "c"})

    perfect = ndcg_at_k(["a", "c", "x", "y"], relevant, 4)
    assert perfect == 1.0

    spread = ndcg_at_k(["a", "x", "c", "y"], relevant, 4)
    assert spread == (1 + 0.5) / (1 + 1 / math.log2(3))
    assert 0 < spread < perfect

    assert ndcg_at_k(["x", "y"], relevant, 4) == 0.0


def test_an_unlabelled_question_scores_as_not_applicable() -> None:
    # NaN, not zero: a question nobody labelled says nothing about the
    # retriever, and averaging it in as a miss punishes the run for the state
    # of its dataset.
    assert recall_at_k(["a"], frozenset(), 8) != recall_at_k(["a"], frozenset(), 8)
    assert precision_at_k(["a"], frozenset(), 8) != precision_at_k(
        ["a"], frozenset(), 8
    )
    assert ndcg_at_k(["a"], frozenset(), 8) != ndcg_at_k(["a"], frozenset(), 8)
    assert reciprocal_rank(["a"], frozenset()) != reciprocal_rank(["a"], frozenset())


def test_ranks_distinguish_demoted_from_vanished() -> None:
    # The whole reason this column exists: "fell from 2 to 9" and "gone
    # entirely" have completely different causes.
    assert ranks_of(["x", "a", "y"], frozenset({"a", "missing"})) == {
        "a": 2,
        "missing": None,
    }


# ───────────────────────────────────────────────────────────────── answers ──


def test_assertions_are_case_insensitive_and_report_what_failed() -> None:
    ok, missing = includes("Sessions cap at 12 hours.", ["12", "SESSIONS"])
    assert ok and missing == []

    ok, missing = includes("Sessions cap at 12 hours.", ["24"])
    assert not ok and missing == ["24"]

    ok, present = excludes("No street address here.", ["street"])
    assert not ok and present == ["street"]


def test_an_invented_citation_scores_zero() -> None:
    # A marker pointing at nothing reads to the user exactly like a real one.
    assert citation_accuracy(["c1", "ghost"], ["c1", "c2"]) == 0.5
    assert citation_accuracy(["ghost"], ["c1"]) == 0.0
    assert citation_accuracy(["c1", "c2"], ["c1", "c2"]) == 1.0


def test_ground_truth_raises_the_bar_from_retrieved_to_relevant() -> None:
    # c2 was retrieved and cited, but it is not what answers the question.
    assert citation_accuracy(["c1", "c2"], ["c1", "c2"], frozenset({"c1"})) == 0.5


def test_no_citations_is_absent_rather_than_zero() -> None:
    # An abstention has no citations and should not be scored as if it lied.
    value = citation_accuracy([], ["c1"])
    assert value != value  # NaN


def test_abstention_is_detected_from_how_the_model_declines() -> None:
    assert abstained("Nothing in the corpus clears the threshold.")
    assert abstained("I could not find that in your documents.")
    assert not abstained("The session cap is 12 hours.")


# ─────────────────────────────────────────────────────────────── aggregate ──


def test_the_accumulator_skips_not_applicable_instead_of_zeroing_it() -> None:
    acc = Accumulator()
    acc.add({"recall_at_k": 1.0, "labelled": True, "hit": True, "latency_ms": 100})
    acc.add({"recall_at_k": float("nan"), "labelled": False, "latency_ms": 200})
    acc.add({"recall_at_k": 0.0, "labelled": True, "hit": False, "latency_ms": 300})

    summary = acc.summary()
    assert summary["recall_at_k"] == 0.5  # mean of two real values, not three
    assert summary["hit_rate"] == 0.5
    assert summary["questions_scored"] == 2
    assert summary["p50_ms"] == 200


def test_pass_rates_count_only_questions_that_asserted_something() -> None:
    acc = Accumulator()
    acc.add({"includes_ok": True})
    acc.add({"includes_ok": False})
    acc.add({})  # this question asserted nothing
    assert acc.summary()["must_include_pass_rate"] == 0.5
    assert acc.summary()["must_not_include_pass_rate"] is None


def test_an_empty_run_reports_nothing_rather_than_zero() -> None:
    summary = Accumulator().summary()
    assert summary["recall_at_k"] is None
    assert summary["hit_rate"] is None
    assert summary["total_cost_usd"] == 0.0


def test_errors_are_counted() -> None:
    acc = Accumulator()
    acc.add({"error": "TimeoutError: upstream"})
    acc.add({"recall_at_k": 1.0, "labelled": True, "hit": True})
    assert acc.summary()["errors"] == 1


# ───────────────────────────────────────────────────────────────── compare ──


def test_pairing_drops_questions_only_one_run_answered() -> None:
    before = {"q1": {"recall_at_k": 0.5, "question": "a"}, "q2": {"recall_at_k": 1.0}}
    after = {"q1": {"recall_at_k": 1.0, "question": "a"}}
    pairs = pair(before, after, "recall_at_k")
    assert len(pairs) == 1
    assert pairs[0].delta == 0.5


def test_pairing_skips_not_applicable_values() -> None:
    assert pair({"q1": {"recall_at_k": float("nan")}}, {"q1": {"recall_at_k": 1.0}},
                "recall_at_k") == []


def test_a_consistent_improvement_is_called_decisive() -> None:
    pairs = [Paired(f"q{i}", before=0.2, after=0.8) for i in range(30)]
    result = bootstrap(pairs, "recall_at_k")
    assert abs(result.delta - 0.6) < 1e-9
    assert result.improved == 30 and result.regressed == 0
    assert result.decisive
    assert result.ci_low > 0
    assert "better by 0.600" in result.verdict()


def test_noise_is_called_inconclusive_rather_than_a_win() -> None:
    # Half up, half down. The honest answer is "this dataset cannot tell them
    # apart", and being able to say so is the entire point of the interval.
    pairs = [
        Paired(f"q{i}", before=0.5, after=0.5 + (0.4 if i % 2 else -0.4))
        for i in range(20)
    ]
    result = bootstrap(pairs, "recall_at_k")
    assert not result.decisive
    assert "inconclusive" in result.verdict()


def test_a_mean_that_rose_on_one_question_is_not_hidden() -> None:
    # One big win, six small losses: the mean goes up, and the counts are what
    # stop someone shipping it.
    pairs = [Paired("winner", 0.0, 1.0)] + [
        Paired(f"loser{i}", 0.5, 0.4) for i in range(6)
    ]
    result = bootstrap(pairs, "recall_at_k")
    assert result.delta > 0
    assert result.improved == 1 and result.regressed == 6
    assert len(result.biggest_losses) == 3


def test_the_interval_is_reproducible() -> None:
    # An interval that moves when you re-run it invites re-rolling until it
    # says what you hoped.
    pairs = [Paired(f"q{i}", 0.3, 0.5 if i % 3 else 0.1) for i in range(25)]
    first = bootstrap(pairs, "mrr")
    second = bootstrap(pairs, "mrr")
    assert (first.ci_low, first.ci_high) == (second.ci_low, second.ci_high)


def test_comparing_nothing_is_not_a_crash() -> None:
    result = bootstrap([], "recall_at_k")
    assert result.n == 0
    assert result.verdict() == "no questions in common"


# ─────────────────────────────────────────────────────────── dataset files ──


def _dataset(**question: object) -> dict[str, object]:
    return {"name": "d", "questions": [{"question": "q?", **question}]}


def test_a_typo_in_a_field_name_is_an_error_not_a_shrug() -> None:
    # `must_includes` would sit in a file for months asserting nothing.
    problems = validate(_dataset(must_includes=["12"]), "f.yaml")
    assert any("unknown field 'must_includes'" in p for p in problems)


def test_duplicate_questions_are_rejected_because_upsert_keys_on_the_text() -> None:
    data = {"name": "d", "questions": [{"question": "same"}, {"question": "same"}]}
    assert any("duplicates" in p for p in validate(data, "f.yaml"))


def test_a_bad_uuid_is_caught_before_anything_is_written() -> None:
    problems = validate(_dataset(expected_document_ids=["not-a-uuid"]), "f.yaml")
    assert any("not a UUID" in p for p in problems)


def test_unanswerable_with_expected_documents_is_contradictory() -> None:
    problems = validate(
        _dataset(difficulty="unanswerable", expected_documents=["Auth decisions"]),
        "f.yaml",
    )
    assert any("unanswerable" in p for p in problems)


def test_an_unknown_difficulty_is_rejected() -> None:
    problems = validate(_dataset(difficulty="tricky"), "f.yaml")
    assert any("difficulty 'tricky'" in p for p in problems)


def test_every_problem_is_reported_at_once() -> None:
    # Eleven round trips versus one edit.
    data = {"name": "", "questions": [{"question": "", "nope": 1, "difficulty": "x"}]}
    assert len(validate(data, "f.yaml")) >= 4


def test_the_shipped_starter_dataset_is_valid() -> None:
    path = Path(SERVICE_ROOT) / "db" / "eval" / "smoke.yaml"
    assert path.is_file(), "the starter dataset should ship with the repo"
    assert validate(load_file(path), path.name) == []
