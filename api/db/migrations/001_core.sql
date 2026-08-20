-- ============================================================================
-- 001_core — identity, tenancy, sources, documents, chunks.
--
-- The load-bearing decision in this file is that `acl_groups` is denormalised
-- from documents onto chunks. Permission filtering then happens inside the same
-- index scan that does retrieval, before anything is ranked — rather than as a
-- post-filter over a shortlist that has already discarded rows the user was
-- allowed to see. See 002_search.sql.
-- ============================================================================

create extension if not exists vector;
create extension if not exists pg_trgm;
create extension if not exists pgcrypto;

-- ─────────────────────────────────────────────────────────────── identity ───

create table if not exists tenants (
  id          uuid primary key default gen_random_uuid(),
  slug        text not null unique,
  name        text not null,
  created_at  timestamptz not null default now()
);

-- Email is stored lower-cased by the application; the unique index below is
-- therefore case-insensitive in practice without depending on citext.
create table if not exists users (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  email       text not null,
  name        text,
  is_admin    boolean not null default false,
  created_at  timestamptz not null default now(),
  unique (tenant_id, email)
);

-- Groups are the unit of access. A document names the group keys that may see
-- it; a user's effective keys come from their memberships. Keys are text rather
-- than uuids so a connector can map an upstream group — "engineering", a GitHub
-- team slug, a Slack channel id — onto an Atlas group with no lookup table.
create table if not exists groups (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  key         text not null,
  name        text not null,
  created_at  timestamptz not null default now(),
  unique (tenant_id, key)
);

create table if not exists group_members (
  group_id    uuid not null references groups(id) on delete cascade,
  user_id     uuid not null references users(id) on delete cascade,
  created_at  timestamptz not null default now(),
  primary key (group_id, user_id)
);

create index if not exists group_members_user on group_members (user_id);

-- ──────────────────────────────────────────────────────────────── sources ───

do $$ begin
  create type source_kind as enum
    ('upload','github','web','notion','slack','jira','gdrive');
exception when duplicate_object then null; end $$;

do $$ begin
  create type sync_status as enum ('idle','queued','syncing','error');
exception when duplicate_object then null; end $$;

create table if not exists sources (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null references tenants(id) on delete cascade,
  kind               source_kind not null,
  name               text not null,

  -- Connector-specific settings: repo, branch, include/exclude globs, base url.
  -- Credentials live here too; the API never serialises this column to a client.
  config             jsonb not null default '{}'::jsonb,

  -- Opaque resume point. GitHub stores a commit sha, web a sitemap etag, Slack
  -- a per-channel cursor. Owned entirely by the connector.
  cursor             jsonb not null default '{}'::jsonb,

  -- ACL stamped onto every document this source produces, unless the connector
  -- derives a finer one per document.
  default_acl_groups text[] not null default '{}',
  default_acl_public boolean not null default false,

  status             sync_status not null default 'idle',
  last_sync_at       timestamptz,
  last_error         text,
  document_count     integer not null default 0,

  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (tenant_id, name)
);

create index if not exists sources_tenant on sources (tenant_id, created_at desc);

-- ────────────────────────────────────────────────────────────── documents ───

do $$ begin
  create type ingest_stage as enum
    ('queued','fetching','extracting','chunking','embedding','indexing',
     'ready','failed','skipped');
exception when duplicate_object then null; end $$;

create table if not exists documents (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  source_id         uuid not null references sources(id) on delete cascade,

  -- Stable identifier *within* the source: a repo-relative path, a Notion page
  -- id, a URL. Paired with source_id this is what makes re-sync idempotent.
  external_id       text not null,
  uri               text,

  title             text not null,
  kind              text not null,
  mime              text,
  lang              text,

  -- sha256 of the normalised extracted text. Equal hash and equal versions
  -- means a re-sync is a no-op and the document is not re-embedded.
  content_hash      text,
  parser_version    integer not null default 1,
  chunker_version   integer not null default 1,
  embedding_version integer not null default 1,

  size_bytes        bigint  not null default 0,
  chunk_count       integer not null default 0,
  token_count       integer not null default 0,

  stage             ingest_stage not null default 'queued',
  progress          real not null default 0,
  error             text,

  -- Group keys permitted to retrieve this document. Empty and not public means
  -- nobody but an admin can reach it.
  acl_groups        text[] not null default '{}',
  acl_public        boolean not null default false,

  -- Connector-owned: repo, path, branch, author, labels, channel. Queried
  -- through the GIN index below for metadata filters.
  metadata          jsonb not null default '{}'::jsonb,

  source_updated_at timestamptz,
  indexed_at        timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),

  unique (source_id, external_id)
);

create index if not exists documents_tenant_created
  on documents (tenant_id, created_at desc);
create index if not exists documents_source_stage
  on documents (source_id, stage);
create index if not exists documents_metadata
  on documents using gin (metadata jsonb_path_ops);
create index if not exists documents_title_trgm
  on documents using gin (title gin_trgm_ops);

-- ───────────────────────────────────────────────────────────────── chunks ───

-- NOTE: vector(768) must equal ATLAS_EMBEDDING_DIM. A mismatch fails loudly at
-- insert with a dimension error rather than degrading retrieval silently.
create table if not exists chunks (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  document_id       uuid not null references documents(id) on delete cascade,

  ordinal           integer not null,
  page              integer,
  -- Breadcrumb: "Title › Section › Subsection", or for code,
  -- "repo/path.py › class TokenService › def refresh". Kept out of `content` so
  -- citation snippets stay clean, but folded into the embedded text and the
  -- tsvector so both channels can match on it.
  heading           text,

  content           text not null,
  -- Exactly what was embedded: breadcrumb + optional generated context +
  -- content. Stored so a retrieval failure can be diagnosed against the real
  -- model input rather than a reconstruction of it.
  context_text      text not null,

  token_count       integer not null default 0,
  embedding         vector(768),
  embedding_version integer not null default 1,

  fts tsvector generated always as (
    to_tsvector('english', coalesce(heading, '') || ' ' || content)
  ) stored,

  -- Lexeme count, for BM25's length normalisation. Recomputed from `content`
  -- rather than reading `fts`, since a generated column may not reference
  -- another generated column.
  fts_len integer generated always as (
    length(to_tsvector('english', coalesce(heading, '') || ' ' || content))
  ) stored,

  -- Denormalised from documents. This is what lets the permission predicate sit
  -- inside the retrieval scan. Kept in step by the trigger below.
  acl_groups        text[] not null default '{}',
  acl_public        boolean not null default false,

  metadata          jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),

  unique (document_id, ordinal)
);

-- Build after bulk load where possible; incremental HNSW construction is
-- several times slower than building once over a populated table.
create index if not exists chunks_embedding_hnsw
  on chunks using hnsw (embedding vector_cosine_ops)
  with (m = 16, ef_construction = 64);

create index if not exists chunks_fts_gin on chunks using gin (fts);

-- The permission predicate is `acl_public or acl_groups && $1`. GIN over the
-- array makes the overlap an index condition rather than a filter.
create index if not exists chunks_acl_gin on chunks using gin (acl_groups);

create index if not exists chunks_tenant_doc on chunks (tenant_id, document_id);
create index if not exists chunks_embedding_version
  on chunks (tenant_id, embedding_version);

-- A document's ACL changing must not leave its chunks retrievable under the old
-- one. In a trigger rather than in application code, so an ACL change arriving
-- by any path — connector, admin UI, manual SQL — is enforced.
create or replace function sync_chunk_acl() returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if new.acl_groups is distinct from old.acl_groups
     or new.acl_public is distinct from old.acl_public then
    update chunks
       set acl_groups = new.acl_groups,
           acl_public = new.acl_public
     where document_id = new.id;
  end if;
  return new;
end $$;

drop trigger if exists documents_acl_sync on documents;
create trigger documents_acl_sync
  after update of acl_groups, acl_public on documents
  for each row execute function sync_chunk_acl();

-- ─────────────────────────────────────────────────────── corpus statistics ───
-- BM25 needs N and avgdl. Both are corpus-wide aggregates that would otherwise
-- be a sequential scan per query, so they are cached here and refreshed by the
-- worker after each ingest. Slightly stale values shift every score by the same
-- constant and do not reorder results.

create table if not exists corpus_stats (
  tenant_id   uuid primary key references tenants(id) on delete cascade,
  chunk_count bigint not null default 0,
  avg_len     real   not null default 1,
  updated_at  timestamptz not null default now()
);

create or replace function refresh_corpus_stats(p_tenant uuid) returns void
language plpgsql
set search_path = public, pg_temp
as $$
begin
  insert into corpus_stats (tenant_id, chunk_count, avg_len, updated_at)
  select p_tenant,
         count(*),
         greatest(coalesce(avg(fts_len), 1), 1),
         now()
    from chunks
   where tenant_id = p_tenant
  on conflict (tenant_id) do update
     set chunk_count = excluded.chunk_count,
         avg_len     = excluded.avg_len,
         updated_at  = excluded.updated_at;
end $$;

-- ──────────────────────────────────────────────────────────────────── rls ───
-- Every read and write goes through the Node process using the direct Postgres
-- role, which owns these tables and is not subject to RLS. These policies exist
-- so that exposing PostgREST with the anon key later denies everything by
-- default rather than leaking the corpus.

alter table tenants       enable row level security;
alter table users         enable row level security;
alter table groups        enable row level security;
alter table group_members enable row level security;
alter table sources       enable row level security;
alter table documents     enable row level security;
alter table chunks        enable row level security;

do $$
declare t text;
begin
  foreach t in array array['tenants','users','groups','group_members',
                           'sources','documents','chunks']
  loop
    execute format('drop policy if exists %I_deny_anon on %I', t, t);
    execute format(
      'create policy %I_deny_anon on %I for all to anon, authenticated using (false)',
      t, t);
  end loop;
end $$;

-- ───────────────────────────────────────────────────────────── timestamps ───

create or replace function touch_updated_at() returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists sources_touch on sources;
create trigger sources_touch before update on sources
  for each row execute function touch_updated_at();

drop trigger if exists documents_touch on documents;
create trigger documents_touch before update on documents
  for each row execute function touch_updated_at();
