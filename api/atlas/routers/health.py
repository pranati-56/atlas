"""Health and configuration status.

Deliberately answers even when the database is unreachable — a health endpoint
that 500s when the thing it reports on is broken tells the operator nothing they
did not already know.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from atlas import db
from atlas.config import settings_status
from atlas.connectors import available_kinds
from atlas.jobs import queue
from atlas.llm import EMBEDDING_MODELS, GENERATION_MODELS

router = APIRouter(tags=["health"])

_REQUIRED_EXTENSIONS = ("vector", "pg_trgm", "pgcrypto")


@router.get("/health")
async def health() -> dict[str, Any]:
    config = settings_status()

    database: dict[str, Any] = {"ok": False}
    migrations: list[str] = []
    extensions: dict[str, bool] = {}

    if config.get("ok"):
        try:
            await db.fetchval("select 1")
            database = {"ok": True}
            rows = await db.fetch(
                "select version from schema_migrations order by version"
            )
            migrations = [r["version"] for r in rows]

            # `vector` missing is the single most common setup failure, and it
            # surfaces much later as an unintelligible cast error — so it is
            # checked by name here rather than left to chance.
            present = {
                r["extname"]
                for r in await db.fetch(
                    "select extname from pg_extension where extname = any($1)",
                    list(_REQUIRED_EXTENSIONS),
                )
            }
            extensions = {e: e in present for e in _REQUIRED_EXTENSIONS}
        except Exception as exc:  # noqa: BLE001 — the message is the payload
            database = {"ok": False, "error": str(exc)}

    ok = bool(config.get("ok")) and database["ok"] and all(extensions.values())
    return {
        "ok": ok,
        "config": config,
        "database": database,
        "extensions": extensions,
        "migrations_applied": migrations,
        "models": {"generation": GENERATION_MODELS, "embedding": EMBEDDING_MODELS},
        "connectors": available_kinds(),
    }


@router.get("/health/queue")
async def queue_health() -> dict[str, Any]:
    """Queue depth and age by kind — the first thing worth watching."""
    return {"queue": await queue.health()}
