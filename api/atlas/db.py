"""The one Postgres pool.

asyncpg rather than an ORM, because the two hottest paths in this system are
things an ORM makes harder: `SELECT ... FOR UPDATE SKIP LOCKED` for the job
queue, and a plpgsql function taking a `vector` argument for retrieval.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from atlas.config import settings

log = logging.getLogger("atlas.db")

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Decode json/jsonb into dicts instead of leaving them as strings."""
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    cfg = settings()
    url = cfg.database_url

    # Supabase's transaction pooler (6543) multiplexes statements across
    # backends, which breaks prepared statements. The session pooler on 5432 is
    # required anyway for the queue and the migrator's advisory lock, but if
    # someone points this at 6543 the failure is otherwise a baffling
    # "prepared statement does not exist" much later.
    kwargs: dict[str, Any] = {}
    if ":6543" in url:
        log.warning(
            "DATABASE_URL points at port 6543 (transaction pooler). "
            "Disabling the statement cache; the job queue and migrator still "
            "need the session pooler on 5432."
        )
        kwargs["statement_cache_size"] = 0

    _pool = await asyncpg.create_pool(
        dsn=url,
        min_size=cfg.database_pool_min,
        max_size=cfg.database_pool_max,
        command_timeout=60,
        init=_init_connection,
        **kwargs,
    )
    assert _pool is not None
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    p, _pool = _pool, None
    await p.close()


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    p = await pool()
    return await p.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    p = await pool()
    return await p.fetchrow(sql, *args)


async def fetchval(sql: str, *args: Any) -> Any:
    p = await pool()
    return await p.fetchval(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    p = await pool()
    return await p.execute(sql, *args)


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """Runs the block in a transaction, rolling back on exception."""
    p = await pool()
    async with p.acquire() as conn, conn.transaction():
        yield conn


def to_vector(embedding: Sequence[float]) -> str:
    """pgvector's text input form.

    A Python list binds as a Postgres array and fails to cast to `vector`, so
    the literal is built explicitly and the call site casts with `$n::vector`.
    """
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"
