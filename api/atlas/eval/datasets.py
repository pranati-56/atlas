"""Loading a dataset file into the eval tables.

A dataset is a file in the repository, not rows someone typed into a database
once. That is the point: it gets reviewed in a pull request, it diffs, and when
a metric moves you can see whether the retriever changed or the questions did.

The one convenience worth having is **titles instead of ids**. Ground truth is
stored as document ids because that is what retrieval returns, but nobody can
hand-write a UUID and a file full of them is unreviewable. So a question may
name documents the way a person would:

    expected_documents: ["Auth decisions", "auth/config.py"]

and import resolves them. Titles are not unique, so a title matching several
documents produces a warning rather than a silent pick — ground truth that looks
labelled and is wrong is worse than no label at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from atlas.eval import store

log = logging.getLogger("atlas.eval.datasets")

#: Everything a question may carry. Anything else is a typo, and silently
#: ignoring it is how `must_includes:` sits in a dataset for months asserting
#: nothing at all.
_QUESTION_KEYS = {
    "question",
    "expected_answer",
    "expected_chunk_ids",
    "expected_document_ids",
    "expected_documents",
    "expected_sources",
    "must_include",
    "must_not_include",
    "tags",
    "difficulty",
    "metadata",
}

_DIFFICULTIES = {"multi-hop", "identifier-lookup", "temporal", "unanswerable"}


@dataclass
class ImportReport:
    dataset: str
    imported: int = 0
    resolved_titles: int = 0
    warnings: list[str] = field(default_factory=list)


def load_file(path: Path) -> dict[str, Any]:
    """YAML or JSON, decided by extension.

    YAML is imported lazily so a JSON-only user never meets the dependency.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ModuleNotFoundError as exc:  # pragma: no cover — declared in deps
            raise RuntimeError(
                "Reading a YAML dataset needs pyyaml. Run `uv sync`, or convert "
                "the file to JSON."
            ) from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: expected a mapping at the top level.")
    return data


def validate(data: dict[str, Any], source: str) -> list[str]:
    """Every structural problem, before anything is written.

    Reporting all of them at once rather than dying on the first is the
    difference between one edit and eleven round trips.
    """
    problems: list[str] = []

    if not str(data.get("name") or "").strip():
        problems.append(f"{source}: the dataset needs a `name`.")

    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        problems.append(f"{source}: `questions` must be a non-empty list.")
        return problems

    seen: set[str] = set()
    for i, q in enumerate(questions, start=1):
        where = f"{source}: question {i}"
        if not isinstance(q, dict):
            problems.append(f"{where} is not a mapping.")
            continue

        text = str(q.get("question") or "").strip()
        if not text:
            problems.append(f"{where} has no `question`.")
        elif text in seen:
            # Upsert is keyed on the text, so duplicates in one file would let
            # the last one silently win.
            problems.append(f"{where} duplicates an earlier question.")
        else:
            seen.add(text)

        for key in sorted(set(q) - _QUESTION_KEYS):
            problems.append(f"{where}: unknown field {key!r}.")

        difficulty = q.get("difficulty")
        if difficulty and difficulty not in _DIFFICULTIES:
            problems.append(
                f"{where}: difficulty {difficulty!r} is not one of "
                f"{sorted(_DIFFICULTIES)}."
            )

        for key in ("expected_chunk_ids", "expected_document_ids"):
            for value in q.get(key) or []:
                try:
                    UUID(str(value))
                except ValueError:
                    problems.append(f"{where}: {key} contains {value!r}, not a UUID.")

        if difficulty == "unanswerable" and (
            q.get("expected_chunk_ids")
            or q.get("expected_document_ids")
            or q.get("expected_documents")
        ):
            problems.append(
                f"{where} is marked unanswerable but names expected documents. "
                f"One of the two is wrong."
            )

    return problems


async def import_dataset(
    *, tenant_id: UUID, path: Path, name: str | None = None
) -> ImportReport:
    data = load_file(path)
    problems = validate(data, path.name)
    if problems:
        raise ValueError("\n".join(problems))

    dataset_name = name or str(data["name"]).strip()
    questions: list[dict[str, Any]] = list(data["questions"])
    report = ImportReport(dataset=dataset_name)

    # ── resolve titles to ids, in one round trip ─────────────────────────
    titles = sorted({t for q in questions for t in (q.get("expected_documents") or [])})
    resolved: dict[str, UUID] = {}
    if titles:
        resolved = await store.documents_by_title(tenant_id, titles)
        collisions = await store.title_collisions(tenant_id, titles)

        for title in titles:
            if title not in resolved:
                report.warnings.append(
                    f"no document titled {title!r} — that question will be scored "
                    f"on whatever other ground truth it has, or not at all"
                )
            elif title in collisions:
                report.warnings.append(
                    f"{collisions[title]} documents are titled {title!r}; used the "
                    f"most recent. Use expected_document_ids to be exact."
                )
        report.resolved_titles = len(resolved)

    dataset_id = await store.ensure_dataset(
        tenant_id, dataset_name, data.get("description")
    )

    for q in questions:
        document_ids = [UUID(str(x)) for x in (q.get("expected_document_ids") or [])]
        for title in q.get("expected_documents") or []:
            found = resolved.get(title)
            if found and found not in document_ids:
                document_ids.append(found)

        await store.upsert_question(
            dataset_id,
            {
                "question": str(q["question"]).strip(),
                "expected_answer": q.get("expected_answer"),
                "expected_chunk_ids": [
                    UUID(str(x)) for x in (q.get("expected_chunk_ids") or [])
                ],
                "expected_document_ids": document_ids,
                "expected_sources": q.get("expected_sources") or [],
                "must_include": q.get("must_include") or [],
                "must_not_include": q.get("must_not_include") or [],
                "tags": q.get("tags") or [],
                "difficulty": q.get("difficulty"),
                "metadata": q.get("metadata") or {},
            },
        )
        report.imported += 1

    return report
