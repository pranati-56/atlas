"""The eval CLI:  uv run atlas-eval <command>

    atlas-eval import db/eval/smoke.yaml
    atlas-eval run --label baseline
    atlas-eval run --label alpha-0.3 --alpha 0.3
    atlas-eval compare baseline alpha-0.3
    atlas-eval show baseline
    atlas-eval list

Output is written to be read at a glance and then trusted or not. Where a number
cannot be trusted — one run against another on nineteen questions — the tool
says so, rather than printing an arrow and letting the reader supply the
confidence themselves.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from atlas import repo
from atlas.db import close_pool
from atlas.eval import compare as compare_mod
from atlas.eval import datasets as datasets_mod
from atlas.eval import store
from atlas.eval.runner import RunConfig, run_dataset
from atlas.retrieval import RetrievalParams

DEFAULT_TENANT = "default"

#: What `compare` reports on. Retrieval first, because it is upstream of
#: everything: an answer cannot be right about a passage nobody retrieved.
COMPARED = ("recall_at_k", "mrr", "ndcg", "precision_at_k", "citation_accuracy")


def _fmt(value: Any, places: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{places}f}"
    return str(value)


async def _tenant_id(slug: str) -> Any:
    tenant = await repo.get_tenant_by_slug(slug)
    if tenant is None:
        raise SystemExit(
            f"No tenant {slug!r}. Run `uv run atlas-seed` first, or pass --tenant."
        )
    return tenant["id"]


# ────────────────────────────────────────────────────────────────── import ──


async def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"No such file: {path}")

    tenant_id = await _tenant_id(args.tenant)
    report = await datasets_mod.import_dataset(
        tenant_id=tenant_id, path=path, name=args.dataset
    )

    print(f"{report.imported} question(s) into dataset {report.dataset!r}")
    if report.resolved_titles:
        print(f"{report.resolved_titles} document title(s) resolved to ids")
    for warning in report.warnings:
        print(f"  warning: {warning}", file=sys.stderr)
    return 0


# ───────────────────────────────────────────────────────────────────── run ──


async def cmd_run(args: argparse.Namespace) -> int:
    defaults = RetrievalParams.defaults()
    params = RetrievalParams(
        alpha=args.alpha if args.alpha is not None else defaults.alpha,
        top_k=args.top_k if args.top_k is not None else defaults.top_k,
        candidates=(
            args.candidates if args.candidates is not None else defaults.candidates
        ),
        rrf_k=args.rrf_k if args.rrf_k is not None else defaults.rrf_k,
        min_score=args.min_score if args.min_score is not None else defaults.min_score,
        rerank=args.rerank,
        # An eval question has no conversation history to resolve against.
        rewrite_query=False,
    )

    cfg = RunConfig(
        params=params,
        generate=not args.no_generate,
        judge=args.judge,
        model=args.model,
        concurrency=args.concurrency,
        tags=args.tag or None,
    )

    saw_progress = False

    def progress(done: int, total: int) -> None:
        nonlocal saw_progress
        saw_progress = True
        print(f"\r  {done}/{total}", end="", file=sys.stderr, flush=True)

    report = await run_dataset(
        tenant_slug=args.tenant,
        dataset=args.dataset,
        label=args.label,
        cfg=cfg,
        as_user=args.as_user,
        on_progress=progress,
    )
    if saw_progress:
        print("", file=sys.stderr)

    m = report.metrics
    print(
        f"\n{report.label}  ·  {report.dataset}  ·  "
        f"{report.question_count} questions"
    )
    print(f"run {report.run_id}")
    print()
    print(f"  recall@k          {_fmt(m['recall_at_k'])}")
    print(f"  precision@k       {_fmt(m['precision_at_k'])}")
    print(f"  mrr               {_fmt(m['mrr'])}")
    print(f"  ndcg              {_fmt(m['ndcg'])}")
    print(f"  hit rate          {_fmt(m['hit_rate'])}")
    if m["citation_accuracy"] is not None:
        print(f"  citation accuracy {_fmt(m['citation_accuracy'])}")
    if m["faithfulness"] is not None:
        print(f"  faithfulness      {_fmt(m['faithfulness'])}")
    for label, key in (
        ("must include", "must_include_pass_rate"),
        ("must not include", "must_not_include_pass_rate"),
        ("abstention", "abstention_accuracy"),
    ):
        if m[key] is not None:
            print(f"  {label:17} {_fmt(m[key])}")
    print()
    print(f"  p50 / p95         {_fmt(m['p50_ms'], 0)} / {_fmt(m['p95_ms'], 0)} ms")
    print(
        f"  cost              ${m['total_cost_usd']:.4f} total, "
        f"${m['cost_per_query']:.4f} per query"
    )

    if m["questions_scored"] == 0:
        print(
            "\n  Nothing was scored: no question in this dataset carries ground "
            "truth.\n  Add expected_documents or expected_sources, then "
            "re-import.",
            file=sys.stderr,
        )
    if cfg.concurrency > 1:
        print(
            f"\n  Latency was measured at concurrency {cfg.concurrency} and is "
            f"contended.\n  Use --concurrency 1 when the timing is the thing you "
            f"care about.",
            file=sys.stderr,
        )
    if report.errors:
        print(f"\n  {len(report.errors)} question(s) errored:", file=sys.stderr)
        for line in report.errors[:5]:
            print(f"    {line}", file=sys.stderr)

    return 0


# ───────────────────────────────────────────────────────────────── compare ──


def _config_diff(before: Any, after: Any) -> None:
    """Only the settings that differ.

    Comparing two runs that differ in nothing is a common and confusing mistake
    — usually the flag did not take — so it gets said out loud.
    """
    a = json.loads(before) if isinstance(before, str) else (before or {})
    b = json.loads(after) if isinstance(after, str) else (after or {})

    changed = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    if not changed:
        print("  configs are identical — this measures run-to-run noise")
        return
    for key in changed:
        print(f"  {key}: {a.get(key)} → {b.get(key)}")


async def cmd_compare(args: argparse.Namespace) -> int:
    tenant_id = await _tenant_id(args.tenant)

    before = await store.find_run(tenant_id, args.before)
    after = await store.find_run(tenant_id, args.after)
    if before is None:
        raise SystemExit(f"No run labelled {args.before!r}.")
    if after is None:
        raise SystemExit(f"No run labelled {args.after!r}.")

    left = await store.load_results(before["id"])
    right = await store.load_results(after["id"])

    print(f"{args.before}  →  {args.after}")
    _config_diff(before["config"], after["config"])
    print()

    for result in compare_mod.compare(left, right, COMPARED):
        if result.n == 0:
            continue
        print(
            f"  {result.metric:18} {result.mean_before:.3f} → "
            f"{result.mean_after:.3f}   {result.verdict()}"
        )
        print(
            f"  {'':18} {result.improved} better, {result.regressed} worse, "
            f"{result.unchanged} unchanged"
        )
        if args.verbose:
            for p in result.biggest_losses:
                print(f"  {'':18}   ↓ {p.delta:+.2f}  {p.question[:56]}")
        print()

    return 0


# ─────────────────────────────────────────────────────────────── list/show ──


async def cmd_list(args: argparse.Namespace) -> int:
    tenant_id = await _tenant_id(args.tenant)

    sets = await store.list_datasets(tenant_id)
    if sets:
        print("datasets")
        for d in sets:
            print(f"  {d['name']:24} {d['question_count']:>4} questions")
        print()

    runs = await store.list_runs(tenant_id, args.dataset, limit=args.limit)
    if not runs:
        print("no runs yet")
        return 0

    print(
        f"  {'label':20} {'dataset':14} {'n':>4} {'recall':>7} {'mrr':>6} "
        f"{'p95':>7}  started"
    )
    for r in runs:
        print(
            f"  {r['label'][:20]:20} {(r['dataset'] or '')[:14]:14} "
            f"{r['question_count']:>4} {_fmt(r['recall_at_k']):>7} "
            f"{_fmt(r['mrr']):>6} {_fmt(r['p95_ms'], 0):>7}  "
            f"{r['started_at']:%Y-%m-%d %H:%M}"
        )
    return 0


async def cmd_show(args: argparse.Namespace) -> int:
    tenant_id = await _tenant_id(args.tenant)
    run = await store.find_run(tenant_id, args.label)
    if run is None:
        raise SystemExit(f"No run labelled {args.label!r}.")

    print(f"{run['label']}  ·  {run['status']}  ·  git {run['git_sha'] or '?'}")
    print()

    rows = await store.failures(run["id"], limit=args.limit)
    if not rows:
        print("  every question retrieved what it was supposed to")
        return 0

    print("  questions to look at first")
    for row in rows:
        print(f"\n  {row['question'][:74]}")
        if row["error"]:
            print(f"    error: {row['error']}")
            continue
        print(f"    recall {_fmt(row['recall'])}")
        raw = row["ranks"]
        ranks = json.loads(raw) if isinstance(raw, str) else (raw or {})
        found = [v for v in ranks.values() if v is not None]
        missing = [k for k, v in ranks.items() if v is None]
        if found:
            print(f"    found at rank: {', '.join(str(v) for v in sorted(found))}")
        if missing:
            print(f"    never retrieved: {len(missing)} expected id(s)")
    return 0


# ───────────────────────────────────────────────────────────────────── cli ──


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas-eval",
        description="Measure retrieval instead of guessing at it.",
    )
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="load a dataset file")
    p_import.add_argument("file")
    p_import.add_argument("--dataset", help="override the name inside the file")
    p_import.set_defaults(fn=cmd_import)

    p_run = sub.add_parser("run", help="run a dataset and record the metrics")
    p_run.add_argument("--dataset", default="smoke")
    p_run.add_argument("--label", required=True, help='"baseline", "alpha-0.3"')
    p_run.add_argument("--alpha", type=float)
    p_run.add_argument("--top-k", type=int, dest="top_k")
    p_run.add_argument("--candidates", type=int)
    p_run.add_argument("--rrf-k", type=int, dest="rrf_k")
    p_run.add_argument("--min-score", type=float, dest="min_score")
    p_run.add_argument("--rerank", action="store_true")
    p_run.add_argument(
        "--no-generate",
        action="store_true",
        help="retrieval only — no model calls, no cost",
    )
    p_run.add_argument(
        "--judge",
        action="store_true",
        help="score faithfulness with an LLM judge (costs money, is noisy)",
    )
    p_run.add_argument("--model")
    p_run.add_argument("--concurrency", type=int, default=4)
    p_run.add_argument("--tag", action="append", help="only questions with this tag")
    p_run.add_argument(
        "--as",
        dest="as_user",
        default=None,
        help="run as this user — ACLs change what is retrievable",
    )
    p_run.set_defaults(fn=cmd_run)

    p_cmp = sub.add_parser("compare", help="two runs, paired, with an interval")
    p_cmp.add_argument("before")
    p_cmp.add_argument("after")
    p_cmp.add_argument("-v", "--verbose", action="store_true")
    p_cmp.set_defaults(fn=cmd_compare)

    p_list = sub.add_parser("list", help="datasets and recent runs")
    p_list.add_argument("--dataset")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(fn=cmd_list)

    p_show = sub.add_parser("show", help="what went wrong in a run")
    p_show.add_argument("label")
    p_show.add_argument("--limit", type=int, default=15)
    p_show.set_defaults(fn=cmd_show)

    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(await args.fn(args))
    except ValueError as exc:
        # Bad dataset, missing tenant, unknown user — every one of these
        # messages is written to be acted on, so print it, not a traceback.
        print(f"\n{exc}", file=sys.stderr)
        return 1


def run() -> None:
    code = 1
    try:
        code = asyncio.run(main())
    finally:
        asyncio.run(close_pool())
    raise SystemExit(code)


if __name__ == "__main__":
    run()
