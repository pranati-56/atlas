-- 009_search_path_extensions — let hybrid_search find pgvector in either schema.
--
-- 001 creates `vector` with a bare `create extension`, which puts it in the
-- first schema on the migrating session's search_path: `public`, on a fresh
-- Supabase project. But Supabase's dashboard installs extensions into
-- `extensions`, and its Security Advisor recommends moving them there. The
-- column and its HNSW index survive either placement — they hold the type by
-- oid — but hybrid_search pins `search_path = public, pg_temp`, and plpgsql
-- resolves the `<=>` in its body at call time against that pinned path. With
-- pgvector in `extensions` the operator does not exist from inside the
-- function: migrate passes, /health is green (it checks extensions by name, not
-- by schema), and every search fails.
--
-- The pin itself is right — an unpinned search_path is how a function gets
-- pointed at someone else's objects — so this widens it rather than dropping
-- it. `extensions` goes after `public`, so nothing that resolves today changes
-- meaning; and a search_path entry naming a schema that does not exist is
-- skipped, so on a plain Postgres with no `extensions` schema this is inert.
--
-- Altered through pg_proc rather than by signature: the signature contains
-- vector(768), and spelling it here would need the very type resolution this
-- migration exists to stop depending on.
--
-- Any later `create or replace function hybrid_search` must carry this
-- search_path forward, or it brings the failure back.

do $$
declare
  fn regprocedure;
begin
  for fn in
    select p.oid::regprocedure
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public'
       and p.proname = 'hybrid_search'
  loop
    execute format(
      'alter function %s set search_path = public, extensions, pg_temp', fn
    );
  end loop;

  if not found then
    raise exception 'hybrid_search() does not exist; 002_search.sql must be applied first';
  end if;
end $$;
