-- ============================================================================
-- 006_blobs — uploaded bytes, held between the request and the worker.
--
-- The upload endpoint returns as soon as the row exists; the worker extracts
-- and embeds later, in its own process. Something has to hold the file in
-- between, and it cannot be the job payload: a 20 MB PDF base64'd into a jsonb
-- column is a 27 MB row that every queue scan has to skip past.
--
-- So the bytes live here, keyed by document, and the job carries only an id.
-- A separate table rather than a column on `documents` because `documents` is
-- read on every corpus listing, and a TOASTed bytea alongside it would make
-- those scans drag.
--
-- Rows are deleted once the document reaches `ready` — the chunks are the
-- durable artefact, not the original upload. Connector-sourced documents never
-- write here at all; they re-fetch from the upstream on reindex.
-- ============================================================================

create table if not exists document_blobs (
  document_id uuid primary key references documents(id) on delete cascade,
  bytes       bytea not null,
  mime        text,
  size_bytes  bigint generated always as (length(bytes)) stored,
  created_at  timestamptz not null default now()
);

-- Reclaiming space after a failed ingest that nobody retried.
create index if not exists document_blobs_created on document_blobs (created_at);

alter table document_blobs enable row level security;
drop policy if exists document_blobs_deny_anon on document_blobs;
create policy document_blobs_deny_anon on document_blobs
  for all to anon, authenticated using (false);
