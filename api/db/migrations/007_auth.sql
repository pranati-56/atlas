-- Passwords.
--
-- The session cookie already existed and was signed correctly; what was missing
-- was any way to obtain one. Everything below is additive: a user with a null
-- password_hash simply cannot sign in with a password, which is the right
-- default for rows created by the seed or by a future invite flow.

alter table users add column if not exists password_hash text;
alter table users add column if not exists last_login_at timestamptz;

-- Sign-in asks for an email and not a workspace, so the first lookup crosses
-- tenants. Without this index that is a sequential scan on every attempt, which
-- is also the cheapest denial-of-service in the system.
--
-- lower(email) rather than email: addresses are normalised on write, but an
-- index that only works when the caller remembered to lower-case first is an
-- index that will eventually be missed.
create index if not exists users_email_lookup_idx on users (lower(email));

-- A person can hold accounts in more than one workspace with the same address,
-- which the (tenant_id, email) unique constraint already permits. The sign-in
-- route resolves the ambiguity by asking; it must never guess.
