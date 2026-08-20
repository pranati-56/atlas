-- ============================================================================
-- 002_search — permission-aware hybrid retrieval.
--
-- Two channels, fused by weighted reciprocal rank fusion:
--
--   score(d) = alpha / (k + rank_dense) + (1 - alpha) / (k + rank_sparse)
--
-- RRF fuses *ranks*, not scores, which is what makes it safe to combine cosine
-- similarity with BM25 without normalising either into the other's units.
--
-- Two things here are deliberately not the usual demo shortcuts:
--
-- 1. The lexical channel is real BM25, not ts_rank_cd. ts_rank_cd has no IDF
--    and no document-length normalisation, so it rewards long chunks and treats
--    a match on "the" like a match on "AUTH-381". IDF is computed at query time
--    from one GIN probe per query lexeme — few probes, always exact — rather
--    than from a term-frequency table that has to be maintained on every write.
--
-- 2. The permission predicate is inside both channel scans. Filtering after
--    ranking would let an inaccessible chunk consume a slot in the shortlist
--    and silently reduce recall for the rows the user *can* see. There is no
--    path through this function that ranks a row the caller may not read.
-- ============================================================================

-- Row shapes passed between the stages inside the function.
do $$ begin
  create type atlas_hit as (id uuid, score float, rnk int);
exception when duplicate_object then null; end $$;

do $$ begin
  create type atlas_fused as (
    id uuid, ds float, ss float, dr int, sr int, fs float
  );
exception when duplicate_object then null; end $$;

-- Adding a parameter to an existing function creates an overload and the next
-- call becomes ambiguous. Drop every signature first.
do $$
declare r record;
begin
  for r in
    select oid::regprocedure as sig from pg_proc
     where proname in ('hybrid_search','find_mentions')
       and pronamespace = 'public'::regnamespace
  loop
    execute format('drop function %s', r.sig);
  end loop;
end $$;

-- ─────────────────────────────────────────────────────────── hybrid_search ───

create function hybrid_search(
  p_tenant          uuid,
  -- The caller's effective group keys, resolved from group_members before the
  -- call. This function does not trust a user id.
  p_groups          text[],
  p_is_admin        boolean,
  p_query           text,
  p_embedding       vector(768),
  p_match_count     int     default 8,   -- returned after fusion
  p_candidate_count int     default 60,  -- pulled from EACH channel
  p_alpha           float   default 0.5, -- 1 = pure vector, 0 = pure lexical
  p_rrf_k           int     default 60,
  p_min_score       float   default 0,
  p_document_ids    uuid[]  default null,
  p_source_ids      uuid[]  default null,
  p_metadata_filter jsonb   default null,
  p_ef_search       int     default 100,
  -- true  → return the whole union of both candidate lists so the debugger can
  --         render the two channels disagreeing
  -- false → return only the fused top-k above p_min_score
  p_with_candidates boolean default false
)
returns table (
  chunk_id       uuid,
  document_id    uuid,
  document_title text,
  document_uri   text,
  source_id      uuid,
  source_kind    text,
  external_id    text,
  ordinal        int,
  page           int,
  heading        text,
  content        text,
  metadata       jsonb,
  dense_score    float,
  sparse_score   float,
  fused_score    float,
  dense_rank     int,
  sparse_rank    int,
  dense_ms       float,
  sparse_ms      float,
  fuse_ms        float
)
language plpgsql
stable
-- Pinned: an unqualified reference must never resolve into a caller-controlled
-- schema.
set search_path = public, pg_temp
as $$
declare
  k1        constant float := 1.2;
  b         constant float := 0.75;
  -- Lexical prefilter depth. BM25 cannot be an index condition, so the GIN
  -- match is narrowed by ts_rank_cd first and only this many rows are rescored.
  -- Wide enough that rescoring can reorder freely; bounded so a query holding a
  -- high-document-frequency term does not rescore the whole corpus.
  prefilter constant int := 5;

  dense_hits  atlas_hit[];
  sparse_hits atlas_hit[];
  fused_hits  atlas_fused[];
  tsq         tsquery;
  n_chunks    bigint;
  avgdl       real;
  t0 timestamptz; t1 timestamptz; t2 timestamptz; t3 timestamptz;
  d_ms float; s_ms float; f_ms float;
begin
  -- Per-query recall knob for HNSW. Must be at least the number of rows
  -- fetched, or the graph walk stops early and quietly returns fewer good
  -- candidates than asked for. Local: reverts at end of transaction.
  perform set_config('hnsw.ef_search',
                     greatest(p_ef_search, p_candidate_count)::text, true);

  tsq := websearch_to_tsquery('english', coalesce(p_query, ''));

  select cs.chunk_count, cs.avg_len into n_chunks, avgdl
    from corpus_stats cs where cs.tenant_id = p_tenant;
  -- No stats row yet (first ingest still running): fall back to values that
  -- make BM25 degrade to a plain TF score rather than divide by zero.
  n_chunks := greatest(coalesce(n_chunks, 1), 1);
  avgdl    := greatest(coalesce(avgdl, 1), 1);

  -- ──────────────────────────────────────────────────── channel 1: dense ───
  -- The LIMIT is applied in the inner subquery and row_number() only in the
  -- outer one. Ranking before limiting would force a sort of every matching row
  -- — window functions are evaluated before ORDER BY/LIMIT — and the HNSW index
  -- would go unused on exactly the query it exists for.
  t0 := clock_timestamp();
  select coalesce(array_agg((q.id, q.score, q.rnk)::atlas_hit), '{}'::atlas_hit[])
    into dense_hits
  from (
    select t.id, t.score, (row_number() over (order by t.distance))::int as rnk
    from (
      select c.id,
             (c.embedding <=> p_embedding) as distance,
             (1 - (c.embedding <=> p_embedding))::float as score
        from chunks c
       where c.embedding is not null
         and c.tenant_id = p_tenant
         and (p_is_admin or c.acl_public or c.acl_groups && p_groups)
         and (p_document_ids is null or c.document_id = any(p_document_ids))
         and (p_metadata_filter is null or c.metadata @> p_metadata_filter)
         and (p_source_ids is null or exists (
               select 1 from documents d
                where d.id = c.document_id and d.source_id = any(p_source_ids)))
       order by c.embedding <=> p_embedding
       limit p_candidate_count
    ) t
  ) q;
  t1 := clock_timestamp();
  d_ms := extract(epoch from (t1 - t0)) * 1000;

  -- ─────────────────────────────────────────── channel 2: lexical, BM25 ───
  -- Stage one narrows with the GIN index; stage two rescores that shortlist
  -- with BM25. Splitting it is what keeps a high-document-frequency term from
  -- turning into a full scan.
  select coalesce(array_agg((q.id, q.score, q.rnk)::atlas_hit), '{}'::atlas_hit[])
    into sparse_hits
  from (
    select t.id, t.score, (row_number() over (order by t.score desc))::int as rnk
    from (
      with terms as (
        -- Lexemes of the query, already stemmed by to_tsvector. Rebuilt into a
        -- tsquery with %L so to_tsquery cannot stem an already-stemmed token a
        -- second time.
        select distinct v.lexeme as term
          from unnest(to_tsvector('english', coalesce(p_query, ''))) v
      ),
      idf as (
        select s.term, ln(1 + ((n_chunks - s.df + 0.5) / (s.df + 0.5))) as w
          from (
            select t.term,
                   (select count(*) from chunks c2
                     where c2.tenant_id = p_tenant
                       and (p_is_admin or c2.acl_public
                            or c2.acl_groups && p_groups)
                       and c2.fts @@ format('%L', t.term)::tsquery
                   )::float as df
              from terms t
          ) s
      ),
      candidates as (
        select c.id, c.fts, c.fts_len
          from chunks c
         where c.fts @@ tsq
           and c.tenant_id = p_tenant
           and (p_is_admin or c.acl_public or c.acl_groups && p_groups)
           and (p_document_ids is null or c.document_id = any(p_document_ids))
           and (p_metadata_filter is null or c.metadata @> p_metadata_filter)
           and (p_source_ids is null or exists (
                 select 1 from documents d
                  where d.id = c.document_id and d.source_id = any(p_source_ids)))
         order by ts_rank_cd(c.fts, tsq) desc
         limit p_candidate_count * prefilter
      )
      select cd.id,
             coalesce(sum(
               i.w * (tf.f * (k1 + 1))
                   / (tf.f + k1 * (1 - b + b * (cd.fts_len::float / avgdl)))
             ), 0)::float as score
        from candidates cd
        cross join idf i
        cross join lateral (
          select coalesce((
            select array_length(u.positions, 1)::float
              from unnest(cd.fts) u where u.lexeme = i.term
          ), 0) as f
        ) tf
       where tf.f > 0
       group by cd.id
       order by score desc
       limit p_candidate_count
    ) t
  ) q;
  t2 := clock_timestamp();
  s_ms := extract(epoch from (t2 - t1)) * 1000;

  -- ────────────────────────────────────────────────────────────── fusion ───
  -- FULL OUTER JOIN, so a passage found by only one channel still competes; its
  -- missing term contributes zero rather than disqualifying the row. That is
  -- the entire point of running two channels.
  select coalesce(
           array_agg((x.id, x.ds, x.ss, x.dr, x.sr, x.fs)::atlas_fused
                     order by x.fs desc),
           '{}'::atlas_fused[])
    into fused_hits
  from (
    select coalesce(d.id, s.id) as id,
           d.score as ds, s.score as ss, d.rnk as dr, s.rnk as sr,
           (coalesce(p_alpha / (p_rrf_k + d.rnk), 0) +
            coalesce((1 - p_alpha) / (p_rrf_k + s.rnk), 0))::float as fs
      from unnest(dense_hits) d
      full outer join unnest(sparse_hits) s on d.id = s.id
  ) x;
  t3 := clock_timestamp();
  f_ms := extract(epoch from (t3 - t2)) * 1000;

  return query
  select c.id, c.document_id, doc.title, doc.uri, doc.source_id,
         src.kind::text, doc.external_id,
         c.ordinal, c.page, c.heading, c.content, c.metadata,
         f.ds, f.ss, f.fs, f.dr, f.sr,
         d_ms, s_ms, f_ms
    from unnest(fused_hits) f
    join chunks    c   on c.id   = f.id
    join documents doc on doc.id = c.document_id
    join sources   src on src.id = doc.source_id
   where f.fs >= (case when p_with_candidates then 0::float else p_min_score end)
   order by f.fs desc
   limit (case when p_with_candidates
               then p_candidate_count * 2 else p_match_count end);
end $$;

-- ────────────────────────────────────────────────────────── find_mentions ───
-- "Find every place we mention the deprecated /v1/auth API."
--
-- Not a retrieval question — an exhaustive lexical one. Embedding similarity is
-- the wrong tool: it returns the *most similar* passages when what is wanted is
-- *all* occurrences, grouped by where they live. ILIKE catches the substring
-- cases a tsvector tokenises away, which is most identifiers — "/v1/auth" and
-- "TokenService.refresh" do not survive the English parser intact.

create function find_mentions(
  p_tenant     uuid,
  p_groups     text[],
  p_is_admin   boolean,
  p_needle     text,
  p_source_ids uuid[] default null,
  p_limit      int    default 200
)
returns table (
  chunk_id       uuid,
  document_id    uuid,
  document_title text,
  document_uri   text,
  source_id      uuid,
  source_kind    text,
  external_id    text,
  page           int,
  heading        text,
  -- 0-based offset of the first hit inside `content`, so the UI can deep-link
  -- to the exact line rather than to the chunk.
  match_offset   int,
  snippet        text,
  occurrences    int,
  metadata       jsonb
)
language sql
stable
set search_path = public, pg_temp
as $$
  select c.id, c.document_id, doc.title, doc.uri, doc.source_id,
         src.kind::text, doc.external_id, c.page, c.heading,
         (position(lower(p_needle) in lower(c.content)) - 1)::int,
         -- 120 characters either side of the first hit, whitespace collapsed.
         regexp_replace(
           substring(c.content
                     from greatest(1, position(lower(p_needle) in lower(c.content)) - 120)
                     for  length(p_needle) + 240),
           '\s+', ' ', 'g'),
         ((length(lower(c.content)) -
           length(replace(lower(c.content), lower(p_needle), '')))
          / nullif(length(p_needle), 0))::int,
         c.metadata
    from chunks c
    join documents doc on doc.id = c.document_id
    join sources   src on src.id = doc.source_id
   where c.tenant_id = p_tenant
     and (p_is_admin or c.acl_public or c.acl_groups && p_groups)
     and (p_source_ids is null or doc.source_id = any(p_source_ids))
     and c.content ilike '%' || p_needle || '%'
   order by src.kind, doc.title, c.ordinal
   limit p_limit;
$$;
