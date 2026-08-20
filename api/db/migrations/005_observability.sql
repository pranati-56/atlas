-- ============================================================================
-- 005_observability — one persisted trace per query.
--
-- The in-request timings the debugger draws vanish when the response closes.
-- Fine for watching one query, useless for the questions that actually come up:
-- which queries got slow last Tuesday, what is p95, which retrievals produced
-- ungrounded answers, what is a query costing.
--
-- So every query writes a trace, spans per stage, the chunks it saw at each
-- stage, and every LLM call with token counts. Cost is stored computed rather
-- than derived at read time — model prices change, and last month's spend
-- should not change with them.
-- ============================================================================

create table if not exists query_traces (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  user_id     uuid references users(id) on delete set null,
  session_id  text,

  question           text not null,
  rewritten_question text,
  plan               jsonb not null default '{}'::jsonb,
  -- Resolved retrieval parameters, so a slow or bad query replays exactly
  -- rather than approximately.
  params             jsonb not null default '{}'::jsonb,

  answer             text,
  -- Fraction of extracted claims the validator found support for.
  groundedness       real,
  citation_count     integer not null default 0,
  unsupported_claims integer not null default 0,

  chunk_count    integer not null default 0,
  context_tokens integer not null default 0,

  total_ms  integer,
  cost_usd  numeric(10,6) not null default 0,

  model           text,
  embedding_model text,
  -- Prompts are versioned. A faithfulness change with no code change is almost
  -- always a prompt edit, and without this column that is unprovable.
  prompt_version  text,

  -- Which budget bound ended the agentic loop: 'complete' | 'max_rounds' |
  -- 'max_tool_calls' | 'token_budget' | 'cost_budget' | 'deadline'.
  stop_reason text,

  error      text,
  created_at timestamptz not null default now()
);

create index if not exists query_traces_tenant_time
  on query_traces (tenant_id, created_at desc);
create index if not exists query_traces_user
  on query_traces (user_id, created_at desc);
-- Partial: "show me the ungrounded answers" is the query this table exists for.
create index if not exists query_traces_low_grounding
  on query_traces (tenant_id, created_at desc)
  where groundedness is not null and groundedness < 0.8;

create table if not exists trace_spans (
  id          bigserial primary key,
  trace_id    uuid not null references query_traces(id) on delete cascade,
  name        text not null,   -- 'plan' | 'embed' | 'dense' | 'sparse' | 'fuse' | …
  seq         integer not null,
  -- Milliseconds from the start of the trace, so spans draw as a real flame
  -- chart rather than a list of durations.
  started_ms  integer not null,
  duration_ms integer not null,
  attributes  jsonb not null default '{}'::jsonb
);

create index if not exists trace_spans_trace on trace_spans (trace_id, seq);

-- What retrieval saw at each stage. Keeping the per-stage rank is what lets the
-- debugger show a chunk entering at dense rank 11, surviving fusion at 4, and
-- being promoted to 1 by the reranker.
--
-- Deliberately no FK to chunks: a trace must outlive the document it cited, or
-- history rewrites itself every time the corpus is pruned.
create table if not exists trace_chunks (
  trace_id uuid not null references query_traces(id) on delete cascade,
  chunk_id uuid not null,
  stage    text not null,  -- 'dense' | 'sparse' | 'fused' | 'reranked' | 'used'
  rank     integer not null,
  score    real,
  primary key (trace_id, stage, chunk_id)
);

create table if not exists llm_calls (
  id       bigserial primary key,
  trace_id uuid references query_traces(id) on delete cascade,
  -- Set for calls made by a job rather than a query — contextualisation during
  -- ingest — so ingest cost is attributable too.
  job_id   bigint,

  provider text not null,
  model    text not null,
  -- 'rewrite' | 'plan' | 'rerank' | 'answer' | 'verify' | 'contextualise'
  purpose  text not null,

  input_tokens  integer not null default 0,
  output_tokens integer not null default 0,
  cached_tokens integer not null default 0,
  cost_usd      numeric(10,6) not null default 0,
  ms            integer,
  error         text,
  created_at    timestamptz not null default now()
);

create index if not exists llm_calls_trace on llm_calls (trace_id);
create index if not exists llm_calls_time on llm_calls (created_at desc);

create table if not exists feedback (
  id         uuid primary key default gen_random_uuid(),
  trace_id   uuid not null references query_traces(id) on delete cascade,
  user_id    uuid references users(id) on delete set null,
  rating     smallint not null check (rating between -1 and 1),
  comment    text,
  created_at timestamptz not null default now(),
  unique (trace_id, user_id)
);

-- Daily rollup. percentile_cont over the raw table is fine at this volume; when
-- it stops being fine this becomes a materialised view refreshed by a job and
-- nothing that reads it has to change.
create or replace view query_stats_daily as
  select tenant_id,
         date_trunc('day', created_at)                          as day,
         count(*)::bigint                                       as queries,
         percentile_cont(0.50) within group (order by total_ms) as p50_ms,
         percentile_cont(0.95) within group (order by total_ms) as p95_ms,
         percentile_cont(0.99) within group (order by total_ms) as p99_ms,
         avg(groundedness)                                      as avg_groundedness,
         sum(cost_usd)                                          as cost_usd,
         avg(cost_usd)                                          as cost_per_query,
         count(*) filter (where error is not null)::bigint      as errors,
         count(*) filter (where unsupported_claims > 0)::bigint as with_unsupported
    from query_traces
   group by tenant_id, date_trunc('day', created_at);

alter table query_traces enable row level security;
alter table trace_spans  enable row level security;
alter table trace_chunks enable row level security;
alter table llm_calls    enable row level security;
alter table feedback     enable row level security;

do $$
declare t text;
begin
  foreach t in array array['query_traces','trace_spans','trace_chunks',
                           'llm_calls','feedback']
  loop
    execute format('drop policy if exists %I_deny_anon on %I', t, t);
    execute format(
      'create policy %I_deny_anon on %I for all to anon, authenticated using (false)', t, t);
  end loop;
end $$;
