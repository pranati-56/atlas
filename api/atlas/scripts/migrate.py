"""Migration runner:  python -m scripts.migrate   (run from api/)

Applies db/migrations/*.sql in filename order, once each, inside a transaction.
Two guards matter:

  - A session-level advisory lock, so two deploys racing to migrate serialise
    instead of both running CREATE INDEX against the same table.
  - A checksum per applied file. Editing a migration that has already run in
    some environment is the mistake that produces schema drift you discover
    months later; this turns it into an error at the next deploy.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time

from atlas.config import MIGRATIONS_DIR
from atlas.db import close_pool, pool

LOCK_KEY = 8_274_113_009  # arbitrary, stable


async def main() -> int:
    if not MIGRATIONS_DIR.is_dir():
        print(f"No migrations directory at {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    p = await pool()
    async with p.acquire() as conn:
        await conn.execute("select pg_advisory_lock($1)", LOCK_KEY)
        try:
            await conn.execute(
                """
                create table if not exists schema_migrations (
                  version    text primary key,
                  checksum   text not null,
                  applied_at timestamptz not null default now()
                )
                """
            )

            applied = {
                r["version"]: r["checksum"]
                for r in await conn.fetch(
                    "select version, checksum from schema_migrations"
                )
            }

            files = sorted(MIGRATIONS_DIR.glob("*.sql"))
            if not files:
                print(f"No .sql files in {MIGRATIONS_DIR}", file=sys.stderr)
                return 1

            ran = 0
            for path in files:
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]
                previous = applied.get(path.name)

                if previous is not None:
                    if previous != checksum:
                        print(
                            f"\n{path.name} has changed since it was applied "
                            f"({previous} -> {checksum}). Add a new migration "
                            f"instead of editing an applied one.",
                            file=sys.stderr,
                        )
                        return 1
                    continue

                print(f"  applying {path.name} ... ", end="", flush=True)
                started = time.monotonic()
                try:
                    async with conn.transaction():
                        await conn.execute(sql)
                        await conn.execute(
                            "insert into schema_migrations (version, checksum) "
                            "values ($1, $2)",
                            path.name,
                            checksum,
                        )
                except Exception as exc:  # noqa: BLE001 — reported to the operator
                    print("failed")
                    print(f"\n{exc}", file=sys.stderr)
                    return 1
                print(f"ok ({int((time.monotonic() - started) * 1000)}ms)")
                ran += 1

            print(
                f"Up to date — {len(files)} migration(s) already applied."
                if ran == 0
                else f"Applied {ran} migration(s)."
            )
            return 0
        finally:
            await conn.execute("select pg_advisory_unlock($1)", LOCK_KEY)


def run() -> None:
    code = 1
    try:
        code = asyncio.run(main())
    finally:
        asyncio.run(close_pool())
    raise SystemExit(code)


if __name__ == "__main__":
    run()
