-- ============================================================================
-- 004_eval — the evaluation store.
--
-- Every retrieval knob this system exposes — alpha, top-k, candidate depth,
-- rrf_k, rerank, decomposition, chunk size — is a hypothesis. Without a
-- labelled set and a metric, tuning them is superstition: you change alpha, the
-- one question you happen to be testing looks better, and you ship a regression
-- across the other ninety.
--
-- Runs are immutable and carry the git sha and full config, so any number
-- printed in a README traces back to the commit and parameters that produced it.
-- ============================================================================

create table if not exists eval_datasets (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  name        text not null,
  description text,
  created_at  timestamptz not null default now(),
  unique (tenant_id, name)
);

create table if not exists eval_questions (
  id         uuid primary key default gen_random_uuid(),
  dataset_id uuid not null references eval_datasets(id) on delete cascade,

  question        text not null,
  expected_answer text,

  -- Ground truth for retrieval. Chunk-level is stricter and preferred;
  -- document-level is what you can realistically label by hand at volume. The
  -- runner scores whichever is populated.
  expected_chunk_ids    uuid[] not null default '{}',
  expected_document_ids uuid[] not null default '{}',
  expected_sources      text[] not null default '{}',

  -- Deterministic assertions on the generated answer, checked with no LLM
  -- judge. A judge is expensive and itself noisy; a substring that must appear
  -- is neither.
  must_include     text[] not null default '{}',
  must_not_include text[] not null default '{}',

  tags       text[] not null default '{}',
  -- 'multi-hop' | 'identifier-lookup' | 'temporal' | 'unanswerable'.
  -- Unanswerable questions are load-bearing: a system that never abstains is a
  -- system that hallucinates on the tail.
  difficulty text,
  metadata   jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists eval_questions_dataset on eval_questions (dataset_id);
create index if not exists eval_questions_tags on eval_questions using gin (tags);

do $$ begin
  create type eval_run_status as enum ('running','complete','failed');
exception when duplicate_object then null; end $$;

create table if not exists eval_runs (
  id         uuid primary key default gen_random_uuid(),
  dataset_id uuid not null references eval_datasets(id) on delete cascade,
  tenant_id  uuid not null references tenants(id) on delete cascade,

  label      text not null,          -- "baseline", "alpha-0.3", "pr-118"
  git_sha    text,

  -- Complete resolved configuration: retrieval params, models, prompt version,
  -- chunker version. This is what makes a run reproducible; a metric without it
  -- is an anecdote.
  config     jsonb not null default '{}'::jsonb,

  status         eval_run_status not null default 'running',
  question_count integer not null default 0,

  -- Written once at the end: recall_at_k, precision_at_k, mrr, ndcg,
  -- faithfulness, citation_accuracy, answer_relevance, p50_ms, p95_ms,
  -- total_cost_usd, cost_per_query.
  metrics    jsonb not null default '{}'::jsonb,

  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  notes       text
);

create index if not exists eval_runs_dataset
  on eval_runs (dataset_id, started_at desc);

create table if not exists eval_results (
  id          uuid primary key default gen_random_uuid(),
  run_id      uuid not null references eval_runs(id) on delete cascade,
  question_id uuid not null references eval_questions(id) on delete cascade,

  -- Rank order matters: Recall@k and MRR are both undefined over an unordered
  -- set.
  retrieved_chunk_ids    uuid[] not null default '{}',
  retrieved_document_ids uuid[] not null default '{}',

  -- Per expected id, the 1-based rank it landed at, or null if never retrieved.
  -- This is the column you read when a metric drops and you need to know
  -- whether the right chunk fell from 2 to 9 or vanished entirely.
  ranks    jsonb not null default '{}'::jsonb,

  answer    text,
  citations jsonb not null default '[]'::jsonb,
  metrics   jsonb not null default '{}'::jsonb,

  latency_ms integer,
  cost_usd   numeric(10,6),
  error      text,
  created_at timestamptz not null default now(),

  unique (run_id, question_id)
);

create index if not exists eval_results_run on eval_results (run_id);

-- `npm run eval -- --compare` reads this, and so does CI when deciding whether
-- a pull request regressed retrieval.
create or replace view eval_run_summary as
  select r.id, r.dataset_id, d.name as dataset, r.label, r.git_sha, r.status,
         r.question_count,
         (r.metrics->>'recall_at_k')::float       as recall_at_k,
         (r.metrics->>'mrr')::float               as mrr,
         (r.metrics->>'ndcg')::float              as ndcg,
         (r.metrics->>'faithfulness')::float      as faithfulness,
         (r.metrics->>'citation_accuracy')::float as citation_accuracy,
         (r.metrics->>'p50_ms')::float            as p50_ms,
         (r.metrics->>'p95_ms')::float            as p95_ms,
         (r.metrics->>'cost_per_query')::float    as cost_per_query,
         r.started_at, r.finished_at
    from eval_runs r
    join eval_datasets d on d.id = r.dataset_id
   order by r.started_at desc;

alter table eval_datasets  enable row level security;
alter table eval_questions enable row level security;
alter table eval_runs      enable row level security;
alter table eval_results   enable row level security;

do $$
declare t text;
begin
  foreach t in array array['eval_datasets','eval_questions','eval_runs','eval_results']
  loop
    execute format('drop policy if exists %I_deny_anon on %I', t, t);
    execute format(
      'create policy %I_deny_anon on %I for all to anon, authenticated using (false)', t, t);
  end loop;
end $$;
