-- ============================================================================
-- 003_jobs — durable work queue.
--
-- Ingestion does not run inside the request that triggered it. A request-scoped
-- worker dies on redeploy, cannot retry, has no visibility and cannot apply
-- backpressure — so a 400-file repository sync becomes a coin flip.
--
-- Postgres is the queue. `FOR UPDATE SKIP LOCKED` gives single-claim semantics
-- across N workers with no broker, and because the queue lives in the same
-- transaction domain as the data, a job can be enqueued atomically with the row
-- that justifies it. Reach for Redis or Kafka when this stops keeping up; at
-- the scale where that happens the schema below still describes what you need.
-- ============================================================================

do $$ begin
  create type job_status as enum
    ('queued','running','succeeded','failed','dead');
exception when duplicate_object then null; end $$;

create table if not exists jobs (
  id            bigserial primary key,
  tenant_id     uuid references tenants(id) on delete cascade,

  kind          text  not null,
  payload       jsonb not null default '{}'::jsonb,

  -- Lower runs first. An interactive re-index beats a nightly full crawl.
  priority      integer not null default 100,

  status        job_status not null default 'queued',
  attempts      integer not null default 0,
  max_attempts  integer not null default 5,

  -- Visibility timeout. A queued job is invisible until now() >= run_after,
  -- which is what implements both scheduling and retry backoff.
  run_after     timestamptz not null default now(),

  -- Lease. A worker that crashes mid-job stops renewing; reclaim_expired_jobs()
  -- returns the row to the queue rather than leaving it 'running' forever.
  locked_by        text,
  lease_expires_at timestamptz,

  last_error    text,

  -- Collapses duplicate work: re-syncing a source while its sync is still
  -- queued must not enqueue a second one. Enforced by the partial unique index
  -- below, which constrains only live rows, so the same key can be enqueued
  -- again once the previous run has finished.
  dedupe_key    text,

  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz
);

create unique index if not exists jobs_dedupe_live
  on jobs (dedupe_key)
  where dedupe_key is not null and status in ('queued','running');

-- The claim query's exact access path: partial, so the index holds only
-- runnable work, and ordered so the planner never has to sort.
create index if not exists jobs_claim
  on jobs (priority, run_after, id)
  where status = 'queued';

create index if not exists jobs_lease
  on jobs (lease_expires_at) where status = 'running';

create index if not exists jobs_tenant_recent
  on jobs (tenant_id, created_at desc);

drop trigger if exists jobs_touch on jobs;
create trigger jobs_touch before update on jobs
  for each row execute function touch_updated_at();

-- ─────────────────────────────────────────────────────────────── claiming ───

-- Atomically claims up to p_batch runnable jobs for one worker.
--
-- SKIP LOCKED is the whole trick: concurrent workers running this statement
-- step over each other's locked rows instead of blocking on them, so throughput
-- scales with worker count and no job is handed to two workers.
create or replace function claim_jobs(
  p_worker  text,
  p_batch   int default 1,
  p_lease_s int default 300,
  p_kinds   text[] default null
)
returns setof jobs
language sql
set search_path = public, pg_temp
as $$
  update jobs j
     set status           = 'running',
         attempts         = j.attempts + 1,
         locked_by        = p_worker,
         lease_expires_at = now() + make_interval(secs => p_lease_s),
         started_at       = coalesce(j.started_at, now())
   where j.id in (
           select id from jobs
            where status = 'queued'
              and run_after <= now()
              and (p_kinds is null or kind = any(p_kinds))
            order by priority, run_after, id
            limit p_batch
            for update skip locked
         )
  returning j.*;
$$;

-- Renews the lease on a long-running job. The worker calls this on a timer;
-- without it a slow-but-healthy ingest would be reclaimed and run twice.
create or replace function heartbeat_job(
  p_id bigint, p_worker text, p_lease_s int default 300
) returns boolean
language sql
set search_path = public, pg_temp
as $$
  update jobs
     set lease_expires_at = now() + make_interval(secs => p_lease_s)
   where id = p_id and locked_by = p_worker and status = 'running'
  returning true;
$$;

create or replace function complete_job(p_id bigint) returns void
language sql
set search_path = public, pg_temp
as $$
  update jobs
     set status = 'succeeded', finished_at = now(),
         locked_by = null, lease_expires_at = null, last_error = null
   where id = p_id;
$$;

-- Fails a job, scheduling a retry unless it is out of attempts.
--
-- Backoff is exponential with jitter and a one-hour ceiling. The jitter matters
-- when a provider outage fails a hundred jobs at once: without it they all
-- retry on the same tick and rate-limit each other into the next failure.
create or replace function fail_job(p_id bigint, p_error text) returns job_status
language plpgsql
set search_path = public, pg_temp
as $$
declare
  j jobs;
  next_status job_status;
  delay_s float;
begin
  select * into j from jobs where id = p_id;
  if not found then return null; end if;

  if j.attempts >= j.max_attempts then
    next_status := 'dead';
    update jobs
       set status = 'dead', finished_at = now(), last_error = p_error,
           locked_by = null, lease_expires_at = null
     where id = p_id;
  else
    next_status := 'queued';
    delay_s := least(3600, 5 * power(2, j.attempts)) * (0.75 + random() * 0.5);
    update jobs
       set status = 'queued', last_error = p_error,
           run_after = now() + make_interval(secs => delay_s),
           locked_by = null, lease_expires_at = null
     where id = p_id;
  end if;

  return next_status;
end $$;

-- Returns jobs whose worker died mid-flight to the queue. Called on a timer by
-- every worker; safe to run concurrently because the UPDATE is the
-- serialisation point.
create or replace function reclaim_expired_jobs() returns integer
language sql
set search_path = public, pg_temp
as $$
  with reclaimed as (
    update jobs
       set status = 'queued', locked_by = null, lease_expires_at = null,
           last_error = coalesce(last_error, 'lease expired — worker vanished'),
           run_after = now()
     where status = 'running' and lease_expires_at < now()
    returning 1
  )
  select count(*)::int from reclaimed;
$$;

-- Queue depth and age by kind — the first thing worth putting on a dashboard.
create or replace view job_queue_health as
  select kind,
         status,
         count(*)::bigint as n,
         min(run_after)   as oldest_runnable,
         max(attempts)    as max_attempts_seen,
         extract(epoch from (now() - min(created_at)))::int as oldest_age_s
    from jobs
   group by kind, status;

alter table jobs enable row level security;
drop policy if exists jobs_deny_anon on jobs;
create policy jobs_deny_anon on jobs for all to anon, authenticated using (false);
