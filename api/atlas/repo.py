"""Queries. One module so the SQL that touches a table lives in one place.

Everything here takes a tenant id. There is no query in Atlas that reads
documents or chunks without one — multi-tenancy that depends on the caller
remembering to filter is multi-tenancy that leaks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from atlas import db
from atlas.ingest.chunking import Chunk
from atlas.ingest.hashing import StoredState

# ──────────────────────────────────────────────────────────────────  identity ──


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is asking, reduced to what retrieval needs.

    `groups` is the resolved set of group keys, not a user id: the SQL
    permission predicate matches on keys, and resolving them once per request
    keeps the retrieval function from having to join membership tables inside
    the hot path.
    """

    user_id: UUID | None
    tenant_id: UUID
    email: str
    groups: list[str]
    is_admin: bool


async def get_tenant_by_slug(slug: str) -> dict[str, Any] | None:
    row = await db.fetchrow("select * from tenants where slug = $1", slug)
    return dict(row) if row else None


async def get_tenant(tenant_id: UUID) -> dict[str, Any] | None:
    """By id, for callers that hold a Principal rather than a slug."""
    row = await db.fetchrow("select * from tenants where id = $1", tenant_id)
    return dict(row) if row else None


async def ensure_tenant(slug: str, name: str) -> UUID:
    return await db.fetchval(
        """
        insert into tenants (slug, name) values ($1, $2)
        on conflict (slug) do update set name = excluded.name
        returning id
        """,
        slug,
        name,
    )


async def ensure_group(tenant_id: UUID, key: str, name: str) -> UUID:
    return await db.fetchval(
        """
        insert into groups (tenant_id, key, name) values ($1, $2, $3)
        on conflict (tenant_id, key) do update set name = excluded.name
        returning id
        """,
        tenant_id,
        key,
        name,
    )


async def ensure_user(
    tenant_id: UUID, email: str, name: str | None = None, is_admin: bool = False
) -> UUID:
    # Email is lower-cased here so the unique index is case-insensitive in
    # practice without depending on citext.
    return await db.fetchval(
        """
        insert into users (tenant_id, email, name, is_admin) values ($1, $2, $3, $4)
        on conflict (tenant_id, email) do update
          set name = coalesce(excluded.name, users.name),
              is_admin = excluded.is_admin
        returning id
        """,
        tenant_id,
        email.strip().lower(),
        name,
        is_admin,
    )


async def add_user_to_group(user_id: UUID, group_id: UUID) -> None:
    await db.execute(
        "insert into group_members (group_id, user_id) values ($1, $2) "
        "on conflict do nothing",
        group_id,
        user_id,
    )


async def first_admin(tenant_slug: str) -> dict[str, Any] | None:
    """The oldest admin in a tenant. Used by the eval runner as its default
    identity, because retrieval is ACL-filtered and an eval has to run as
    somebody."""
    row = await db.fetchrow(
        """
        select u.email
          from users u
          join tenants t on t.id = u.tenant_id
         where t.slug = $1 and u.is_admin
         order by u.created_at
         limit 1
        """,
        tenant_slug,
    )
    return dict(row) if row else None


async def load_principal(tenant_slug: str, email: str) -> Principal | None:
    """Resolves a user and their effective group keys in one round trip."""
    row = await db.fetchrow(
        """
        select u.id as user_id,
               u.tenant_id,
               u.email,
               u.is_admin,
               coalesce(
                 array_agg(g.key) filter (where g.key is not null), '{}'
               ) as groups
          from users u
          join tenants t on t.id = u.tenant_id
          left join group_members m on m.user_id = u.id
          left join groups g on g.id = m.group_id
         where t.slug = $1 and u.email = $2
         group by u.id, u.tenant_id, u.email, u.is_admin
        """,
        tenant_slug,
        email.strip().lower(),
    )
    if row is None:
        return None
    return Principal(
        user_id=row["user_id"],
        tenant_id=row["tenant_id"],
        email=row["email"],
        groups=list(row["groups"]),
        is_admin=row["is_admin"],
    )


# ──────────────────────────────────────────────────────────────────────  auth ──


async def find_accounts_by_email(email: str) -> list[dict[str, Any]]:
    """Every workspace holding this address.

    Sign-in asks for an email and a password, not a workspace slug — nobody
    remembers the slug. `(tenant_id, email)` is unique but email alone is not,
    so one address can legitimately be a member of several workspaces. The route
    resolves that by asking which one; it must never pick for the user.
    """
    rows = await db.fetch(
        """
        select u.id as user_id,
               u.email,
               u.password_hash,
               t.slug as tenant_slug,
               t.name as tenant_name
          from users u
          join tenants t on t.id = u.tenant_id
         where lower(u.email) = lower($1)
         order by t.slug
        """,
        email.strip(),
    )
    return [dict(r) for r in rows]


async def get_account(tenant_slug: str, email: str) -> dict[str, Any] | None:
    row = await db.fetchrow(
        """
        select u.id as user_id, u.email, u.password_hash, t.slug as tenant_slug
          from users u
          join tenants t on t.id = u.tenant_id
         where t.slug = $1 and lower(u.email) = lower($2)
        """,
        tenant_slug,
        email.strip(),
    )
    return dict(row) if row else None


async def set_password(user_id: UUID, password_hash: str) -> None:
    await db.execute(
        "update users set password_hash = $2 where id = $1", user_id, password_hash
    )


async def touch_login(user_id: UUID) -> None:
    """Last seen. Not load-bearing, but the first thing anyone asks when an
    account is disputed."""
    await db.execute(
        "update users set last_login_at = now() where id = $1", user_id
    )


# ───────────────────────────────────────────────────────────────────  sources ──


async def create_source(
    *,
    tenant_id: UUID,
    kind: str,
    name: str,
    config: dict[str, Any] | None = None,
    acl_groups: list[str] | None = None,
    acl_public: bool = False,
) -> dict[str, Any]:
    row = await db.fetchrow(
        """
        insert into sources
          (tenant_id, kind, name, config, default_acl_groups, default_acl_public)
        values ($1, $2::source_kind, $3, $4::jsonb, $5, $6)
        returning *
        """,
        tenant_id,
        kind,
        name,
        json.dumps(config or {}),
        acl_groups or [],
        acl_public,
    )
    assert row is not None
    return dict(row)


async def get_source(tenant_id: UUID, source_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow(
        "select * from sources where id = $1 and tenant_id = $2", source_id, tenant_id
    )
    return dict(row) if row else None


async def list_sources(tenant_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "select * from sources where tenant_id = $1 order by created_at desc",
        tenant_id,
    )
    return [dict(r) for r in rows]


async def set_source_status(
    source_id: UUID, status: str, error: str | None = None
) -> None:
    await db.execute(
        """
        update sources
           set status = $2::sync_status,
               last_error = $3,
               last_sync_at = case when $2 = 'idle' then now() else last_sync_at end
         where id = $1
        """,
        source_id,
        status,
        error,
    )


async def set_source_cursor(source_id: UUID, cursor: dict[str, Any]) -> None:
    await db.execute(
        "update sources set cursor = $2::jsonb where id = $1",
        source_id,
        json.dumps(cursor),
    )


# ─────────────────────────────────────────────────────────────────  documents ──


async def put_blob(document_id: UUID, data: bytes, mime: str) -> None:
    """Parks binary content for the ingest handler to pick up.

    Only used by connectors that pull files rather than text — Drive PDFs, for
    instance. The handler deletes the row once chunks exist, so this table is a
    hand-off buffer and never a store.
    """
    await db.execute(
        """
        insert into document_blobs (document_id, bytes, mime)
        values ($1, $2, $3)
        on conflict (document_id) do update
          set bytes = excluded.bytes,
              mime = excluded.mime
        """,
        document_id,
        data,
        mime,
    )


async def upsert_document(
    *,
    tenant_id: UUID,
    source_id: UUID,
    external_id: str,
    title: str,
    kind: str,
    uri: str | None = None,
    mime: str | None = None,
    size_bytes: int = 0,
    acl_groups: list[str] | None = None,
    acl_public: bool = False,
    metadata: dict[str, Any] | None = None,
    source_updated_at: Any = None,
) -> dict[str, Any]:
    """Idempotent on (source_id, external_id) — the pair a connector can
    reproduce across syncs."""
    row = await db.fetchrow(
        """
        insert into documents
          (tenant_id, source_id, external_id, title, kind, uri, mime, size_bytes,
           acl_groups, acl_public, metadata, source_updated_at, stage, progress)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12,
                'queued', 0)
        on conflict (source_id, external_id) do update
          set title = excluded.title,
              uri = excluded.uri,
              mime = excluded.mime,
              size_bytes = excluded.size_bytes,
              acl_groups = excluded.acl_groups,
              acl_public = excluded.acl_public,
              metadata = excluded.metadata,
              source_updated_at = excluded.source_updated_at
        returning *
        """,
        tenant_id,
        source_id,
        external_id,
        title,
        kind,
        uri,
        mime,
        size_bytes,
        acl_groups or [],
        acl_public,
        json.dumps(metadata or {}),
        source_updated_at,
    )
    assert row is not None
    return dict(row)


async def get_document(tenant_id: UUID, document_id: UUID) -> dict[str, Any] | None:
    row = await db.fetchrow(
        "select * from documents where id = $1 and tenant_id = $2",
        document_id,
        tenant_id,
    )
    return dict(row) if row else None


async def stored_state(document_id: UUID) -> StoredState | None:
    row = await db.fetchrow(
        """
        select content_hash, parser_version, chunker_version, embedding_version
          from documents where id = $1
        """,
        document_id,
    )
    if row is None:
        return None
    return StoredState(
        content_hash=row["content_hash"],
        parser_version=row["parser_version"],
        chunker_version=row["chunker_version"],
        embedding_version=row["embedding_version"],
    )


async def set_stage(
    document_id: UUID,
    stage: str,
    *,
    progress: float | None = None,
    error: str | None = None,
    **columns: Any,
) -> None:
    """Stage and progress are polled by the corpus table, so the pipeline is
    visible while it runs rather than being a spinner that either finishes or
    does not."""
    sets = ["stage = $2::ingest_stage"]
    args: list[Any] = [document_id, stage]

    if progress is not None:
        args.append(progress)
        sets.append(f"progress = ${len(args)}")
    if error is not None or stage == "ready":
        args.append(error)
        sets.append(f"error = ${len(args)}")
    for column, value in columns.items():
        args.append(value)
        sets.append(f"{column} = ${len(args)}")
    if stage == "ready":
        sets.append("indexed_at = now()")

    await db.execute(
        f"update documents set {', '.join(sets)} where id = $1",  # noqa: S608
        *args,
    )


async def list_documents(
    tenant_id: UUID, *, source_id: UUID | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select d.*, s.kind::text as source_kind, s.name as source_name
          from documents d
          join sources s on s.id = d.source_id
         where d.tenant_id = $1
           and ($2::uuid is null or d.source_id = $2)
         order by d.created_at desc
         limit $3
        """,
        tenant_id,
        source_id,
        limit,
    )
    return [dict(r) for r in rows]


async def delete_document(tenant_id: UUID, document_id: UUID) -> bool:
    status = await db.execute(
        "delete from documents where id = $1 and tenant_id = $2", document_id, tenant_id
    )
    return status.endswith(" 1")


# ────────────────────────────────────────────────────────────────────  chunks ──

#: Rows per insert. Large enough to matter, small enough to stay well under
#: asyncpg's 32767 bind-parameter cap per statement.
INSERT_BATCH = 200


async def replace_chunks(
    *,
    tenant_id: UUID,
    document_id: UUID,
    chunks: list[Chunk],
    vectors: list[list[float]],
    acl_groups: list[str],
    acl_public: bool,
    embedding_version: int,
) -> None:
    """Replaces rather than appends, in one transaction.

    A partial write here would leave the document retrievable as a mixture of
    old and new chunks, which is worse than either alone — so the delete and the
    inserts share a transaction and the document is never half-reindexed.
    """
    if len(chunks) != len(vectors):
        raise ValueError(
            f"{len(chunks)} chunks but {len(vectors)} vectors — refusing to write"
        )

    async with db.transaction() as conn:
        await conn.execute("delete from chunks where document_id = $1", document_id)

        for i in range(0, len(chunks), INSERT_BATCH):
            batch = chunks[i : i + INSERT_BATCH]
            await conn.executemany(
                """
                insert into chunks
                  (tenant_id, document_id, ordinal, page, heading, content,
                   context_text, token_count, embedding, embedding_version,
                   acl_groups, acl_public, metadata)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9::vector, $10, $11,
                        $12, $13::jsonb)
                """,
                [
                    (
                        tenant_id,
                        document_id,
                        c.ordinal,
                        c.page,
                        c.heading,
                        c.content,
                        c.context_text,
                        c.token_count,
                        db.to_vector(vectors[i + j]),
                        embedding_version,
                        acl_groups,
                        acl_public,
                        json.dumps(c.metadata),
                    )
                    for j, c in enumerate(batch)
                ],
            )


async def chunks_for_document(document_id: UUID) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        select id, ordinal, page, heading, content, context_text, token_count
          from chunks where document_id = $1 order by ordinal
        """,
        document_id,
    )
    return [dict(r) for r in rows]


async def update_chunk_vectors(
    document_id: UUID, vectors: list[list[float]], embedding_version: int
) -> None:
    """The embed-only reindex path: chunk text and boundaries are unchanged, so
    only the vectors are rewritten."""
    async with db.transaction() as conn:
        rows = await conn.fetch(
            "select id from chunks where document_id = $1 order by ordinal",
            document_id,
        )
        if len(rows) != len(vectors):
            raise ValueError(
                f"{len(rows)} stored chunks but {len(vectors)} vectors — "
                f"the document needs a full reindex, not an embed-only one"
            )
        await conn.executemany(
            "update chunks set embedding = $2::vector, embedding_version = $3 "
            "where id = $1",
            [
                (r["id"], db.to_vector(vectors[i]), embedding_version)
                for i, r in enumerate(rows)
            ],
        )


async def refresh_corpus_stats(tenant_id: UUID) -> None:
    """BM25's N and avgdl. Cheap, and stale values only shift every score by the
    same constant — but a first ingest with no stats row makes IDF meaningless."""
    await db.execute("select refresh_corpus_stats($1)", tenant_id)


async def corpus_counts(tenant_id: UUID) -> dict[str, int]:
    row = await db.fetchrow(
        """
        select (select count(*) from documents where tenant_id = $1) as documents,
               (select count(*) from chunks where tenant_id = $1) as chunks,
               (select count(*) from documents
                 where tenant_id = $1 and stage = 'ready') as ready,
               (select count(*) from documents
                 where tenant_id = $1 and stage = 'failed') as failed
        """,
        tenant_id,
    )
    return {k: int(v) for k, v in dict(row or {}).items()}
