# Setting up Atlas

From a fresh clone to asking questions of your own documents. Atlas is three
processes and a database:

| process | what it does | port |
| --- | --- | --- |
| API (`api/`, FastAPI) | serves requests, enqueues work | 8000 |
| worker (`api/`, same code) | extracts, chunks and embeds documents off the queue | — |
| web (repo root, Next.js) | the UI | 3000 |
| Postgres (Supabase) | schema, vectors, BM25 index and the job queue | — |

The API and the worker read `api/.env`. The frontend reads `.env.local` at the
repo root, and the only thing it needs from that file is where the API is.

---

## 0. Prerequisites

- **Node.js 20+** and npm, for the frontend.
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**, for the
  backend. You don't need to install Python first: uv reads
  `api/.python-version` (3.12) and downloads that version when it isn't already
  on the machine.
- **A Supabase project** (the free tier is enough). Any Postgres that can run
  `create extension vector` will also work. Supabase is the setup this guide
  covers.
- **A Gemini API key** from [Google AI Studio](https://aistudio.google.com/apikey).
  Gemini is the default for both generation and embeddings, so this is the only
  model key you need.

## 1. Clone and install

```bash
git clone <your-repo-url> atlas
cd atlas
npm install
cd api && uv sync && cd ..
```

`uv sync` creates `api/.venv` from the committed `uv.lock`, including the dev
tools (pytest, ruff, mypy). You never activate the virtualenv; run everything in
`api/` through `uv run`.

## 2. Set up Supabase

Atlas uses Supabase only as a Postgres database. It doesn't use Supabase Auth,
the REST Data API, Storage, or the `anon` and `service_role` keys, so you won't
need any of those. Everything below is about getting a database Atlas can
connect to safely.

### 2a. Create the project

1. Sign in at [supabase.com/dashboard](https://supabase.com/dashboard) and
   click **New project**.
2. Choose an organization and give the project a name, for example `atlas`.
3. **Database password:** use the generator, then copy the password into a
   password manager straight away. Supabase won't show it again, and resetting
   it later means updating every `DATABASE_URL` that uses it.
4. **Region:** choose the one closest to where the API will run. For local
   development, that's the one closest to you. Every search makes a database
   round trip, so a distant region adds latency to every request.
5. The **Free** plan is enough. Create the project and wait until the dashboard
   shows it as ready, which takes a minute or two.

### 2b. Get the connection string

1. Click **Connect** at the top of the project page.
2. Copy the **Session pooler** connection string (URI format). It looks like
   `postgresql://postgres.<project-ref>:[YOUR-PASSWORD]@aws-0-<region>.pooler.supabase.com:5432/postgres`.
   Copy it from the dashboard rather than typing it, because the pooler
   hostname varies between projects.
3. Replace the password placeholder with your database password. If the
   password contains `@`, `:`, `/`, `#` or `%`, URL-encode it; for example, `@`
   becomes `%40`.
4. Add `?sslmode=require` to the end of the URI so that the connection is always
   encrypted.

Supabase offers three ways to connect, and only two work for Atlas:

| option | port | network | works for Atlas? |
| --- | --- | --- | --- |
| **Session pooler** | 5432 | IPv4 | **Yes. Use this one;** it works on any network. |
| Direct connection | 5432 | IPv6 only, unless you buy the IPv4 add-on | Yes, if your machine and host support IPv6 |
| Transaction pooler | 6543 | IPv4 | **No** |

> **Why not the transaction pooler?** It can send consecutive statements to
> different backends. That breaks the migrator's advisory lock and the job
> queue's `SELECT … FOR UPDATE SKIP LOCKED`. The symptom is a migrate that hangs
> or jobs that never get picked up.

### 2c. Leave the extensions to the migration

Atlas needs three Postgres extensions: `vector`, `pg_trgm` and `pgcrypto`. The
first migration creates them in the `public` schema, so you don't need to do
anything in the dashboard.

If you've already enabled `vector` from **Database → Extensions**, that's fine.
The dashboard puts it in the `extensions` schema instead of `public`, and
migration 009 lets Atlas's search function find it in either. The Security
Advisor's "Extension in Public" warnings for `vector` and `pg_trgm` are
expected, and you can ignore them.

### 2d. Turn off the Data API

Supabase can expose the tables in `public` as a REST API, and Atlas's tables
live in `public`. The migrations turn on row-level security for those tables
with no policies, so the API would refuse every request. Atlas doesn't use the
Data API, though, so the safer choice is to turn it off completely:

1. Go to **Integrations → Data API**
   (`supabase.com/dashboard/project/_/integrations/data_api/overview`).
2. Switch off **Enable Data API**.

Turning it off also covers `schema_migrations`, the migrator's bookkeeping
table, which has no row-level security. The Security Advisor may flag that
table. With the Data API off, nothing can read it from outside.

### 2e. Stay under the connection limit

In session mode, Supabase limits the number of connections to the project's
**Pool Size**, which you'll find on the Database Settings page
(`supabase.com/dashboard/project/_/database/settings`). On small projects it is
often 15. A client that goes over the limit is refused with
`Max client connections reached`.

The API and the worker each open up to `DATABASE_POOL_MAX` connections, and the
default is 10, so together they can need 20. Keep
`DATABASE_POOL_MAX × (API + worker processes)` a little below the Pool Size,
because migrate and seed briefly need a connection too. With a Pool Size of 15
and one API plus one worker, set `DATABASE_POOL_MAX=6` in step 3.

### 2f. Know what the free plan does

- **Projects pause when idle.** A free project with little database activity
  for about a week is paused. `/health` then reports `database.ok: false` with
  a connection error. Restore the project from the dashboard and wait until it
  shows as active again, which can take a few minutes. Your data is kept.
- **Resetting the password.** Use **Reset database password** on the Database
  Settings page. Then update `DATABASE_URL` in `api/.env` and in every
  deployment, and restart the API and the worker.

### Optional: run Supabase on your own machine

If you'd rather not use a hosted project during development, the Supabase CLI
runs the same Postgres image, with pgvector included, in Docker:

```bash
npx supabase init     # creates a supabase/ folder; run it outside this repo
npx supabase start    # needs Docker running
```

Then use `DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres`.
That is a direct connection with no pooler, so sections 2b, 2d and 2e don't
apply. `npx supabase stop` shuts it down.

## 3. Configure the backend

```bash
cp api/.env.example api/.env               # bash / macOS / Linux
Copy-Item api/.env.example api/.env        # PowerShell
```

Three values are required:

| variable | value |
| --- | --- |
| `DATABASE_URL` | the session-pooler URI from step 2b, ending in `?sslmode=require` |
| `GEMINI_API_KEY` | your AI Studio key |
| `ATLAS_SESSION_SECRET` | a long random string that signs the session cookie (see below) |

Generate the secret with either of these:

```bash
openssl rand -base64 48
```

```bash
cd api && uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use the second on Windows if you don't have `openssl`. Every other variable in
`api/.env.example` has a working default. The one exception: on a free Supabase
project, also set `DATABASE_POOL_MAX=6` (see step 2e).

**Optional — skip the login while developing.** If you set
`ATLAS_DEV_USER=alice@acme.example`, every request is treated as that user and
no sign-in is needed. The API logs a warning on every request while it is set.
Never set it anywhere that isn't your own machine, because it bypasses
authentication entirely.

## 4. Configure the frontend (usually skip)

If the API runs on `http://localhost:8000`, the frontend needs no configuration.
To point the frontend somewhere else:

```bash
cp .env.example .env.local
```

Then set `NEXT_PUBLIC_ATLAS_API` and add the frontend's origin to
`ATLAS_CORS_ORIGINS` in `api/.env`. The defaults already allow
`localhost:3000` and `localhost:3400`.

## 5. Migrate and seed

```bash
cd api
uv run atlas-migrate
uv run atlas-seed
```

The migrator applies `api/db/migrations/*.sql` in filename order, once each, and
records a checksum for every file it applies. Re-running it is safe; it prints
`Up to date` when there is nothing to do.

The seed creates a workspace called **Acme** (slug `default`), three groups and
four users. All four users share the password **`atlas-demo-2026`**. To choose
your own, set `ATLAS_SEED_PASSWORD` before you run the seed.

| user | groups | can see |
| --- | --- | --- |
| `admin@acme.example` | engineering, hr, public | everything; the only one who can add sources |
| `bob@acme.example` | engineering, hr, public | engineering + HR + company-wide |
| `alice@acme.example` | engineering, public | engineering + company-wide |
| `carol@acme.example` | public | company-wide only |

The seed is idempotent. Running it again only resets those four passwords.

## 6. Run it

Use three terminals:

```bash
cd api && uv run uvicorn atlas.main:app --reload --port 8000
```

```bash
cd api && uv run atlas-worker
```

```bash
npm run dev
```

If the worker isn't running, uploads and syncs are accepted but stay `queued`
indefinitely, because the API process never runs jobs itself.

## 7. Check that it works

**The API's health check.** Open <http://localhost:8000/health>. You want to see:

```json
{
  "ok": true,
  "database": { "ok": true },
  "extensions": { "vector": true, "pg_trgm": true, "pgcrypto": true },
  "migrations_applied": ["001_core.sql", "002_search.sql", "…"]
}
```

When `ok` is false, the rest of the response says which part failed: config,
database or extensions. The full API reference is at
<http://localhost:8000/docs>.

**Sign in.** Go to <http://localhost:3000/signin> and sign in as
`admin@acme.example`.

**Index something.** On **Connectors**, add a **Web** source with one or two
URLs, or upload a PDF or Markdown file. Set the groups to `engineering` so
retrieval has something to enforce. A document with no groups that isn't marked
public is visible only to admins. Watch the worker terminal: the document goes
from queued to indexed in a few seconds. <http://localhost:8000/health/queue>
shows the queue depth.

**See the permissions work.** Ask a question about that document as
`alice@acme.example`, then sign out and ask the same question as
`carol@acme.example`. Alice gets cited passages; Carol gets nothing from it.
This happens because the ACL filter runs inside the retrieval scan rather than
being applied afterwards. The debugger drawer shows which channel, dense or
BM25, found each passage.

---

## Connecting real sources

Only an admin can add a source. Credentials you enter in the UI are stored on
that source and are never returned to a browser. The env vars in `api/.env` are
a fallback for when one workspace serves the whole tenant.

| connector | you need | env fallback |
| --- | --- | --- |
| GitHub | `owner/name`, an optional branch, and a fine-grained PAT with **Contents: read** (optional for public repos) | `GITHUB_TOKEN` |
| Slack | a bot token (`xoxb-…`) with `channels:read`, `channels:history` and `users:read`, with the bot invited to each channel; channels it isn't in are skipped without an error | `SLACK_BOT_TOKEN` |
| Notion | an internal integration token (`ntn_…`), with the pages or teamspaces shared with that integration | `NOTION_TOKEN` |
| Google Drive | an OAuth refresh token for the `drive.readonly` scope, plus the client ID and secret it was issued to | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |
| Jira | site URL, account email, an [API token](https://id.atlassian.com/manage-profile/security/api-tokens), and an optional project key | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` |
| Web | a fixed list of URLs. It is not a crawler. | — |

Each connector has one known limitation, documented in its module docstring
under `api/atlas/connectors/`. Check these before you assume the pipeline lost
something:

- **Slack** doesn't pick up new replies to old threads.
- **Drive** and **Notion** don't detect deleted documents.

## Running the checks

```bash
cd api && uv run pytest          # unit tests, no database needed
cd api && uv run ruff check .    # lint
cd api && uv run mypy atlas      # types (strict)
npm run typecheck                # frontend types
npm run build                    # frontend production build
```

## Deploying

The two halves deploy separately. Three settings connect them:
`NEXT_PUBLIC_ATLAS_API`, `ATLAS_CORS_ORIGINS`, and the session cookie settings.

- **API:** a container running `uv sync --no-dev` and then `uv run atlas-api`.
  It listens on `$PORT`, which every managed host injects. Run
  `uv run atlas-migrate` before each deploy. The migrator takes a lock, so two
  deploys running it at once will take turns instead of conflicting.
- **Worker:** the same image running `uv run atlas-worker`, pointed at the same
  database. Each process opens up to `DATABASE_POOL_MAX` connections, so keep
  the total across all processes under your Supabase plan's connection limit.
- **Frontend:** a standard Next.js build. Set `NEXT_PUBLIC_ATLAS_API` **at
  build time**, because Next.js inlines it into the bundle.

Production settings for `api/.env`:

| variable | set to | why |
| --- | --- | --- |
| `ATLAS_CORS_ORIGINS` | your frontend's exact origin | the session is a cookie, so `*` is not allowed |
| `ATLAS_COOKIE_SECURE` | `true` | HTTPS only |
| `ATLAS_COOKIE_SAMESITE` | `none` if the frontend and API are on different sites (e.g. `vercel.app` and `fly.dev`), otherwise `lax` | browsers silently drop a cross-site cookie without it |
| `ATLAS_ALLOW_SIGNUP` | `false` | anyone can create a workspace while it is on |
| `ATLAS_DEV_USER` | **unset** | it bypasses authentication entirely |

---

## Troubleshooting

`/health` is the first thing to check. It answers even when the database is
down, and it reports which part of the setup is wrong.

| symptom | cause | fix |
| --- | --- | --- |
| The browser says it can't reach the API, but `/health` loads fine | The frontend's origin isn't in `ATLAS_CORS_ORIGINS` | Add the exact origin (scheme, host and port) and restart the API |
| Sign-in appears to work, then the next request returns 401 | The browser dropped a cross-site cookie | Set `ATLAS_COOKIE_SAMESITE=none` and `ATLAS_COOKIE_SECURE=true` (this requires HTTPS) |
| `atlas-migrate` hangs, or jobs are never claimed | You're on the transaction pooler (port 6543) | Switch `DATABASE_URL` to the session pooler on port 5432 |
| `… has changed since it was applied` | A migration file was edited after it ran | Revert the edit and put the change in a new numbered migration |
| `extensions.vector: false` | Migrations haven't run, or the first one failed | Run `uv run atlas-migrate` and read the error it prints |
| Every search fails with `operator does not exist: extensions.vector <=> extensions.vector` | `vector` is in the `extensions` schema and migration 009 hasn't been applied | Run `uv run atlas-migrate` |
| Indexing or search fails with `type "vector" does not exist` | `vector` is in a schema your connection's `search_path` doesn't include | In the SQL Editor, run `alter extension vector set schema public;` |
| `Max client connections reached` | The API and worker together are asking for more connections than the session pooler's Pool Size | Lower `DATABASE_POOL_MAX` (step 2e), or raise the Pool Size in Supabase's Database Settings |
| `/health` shows `database.ok: false` with a timeout, and it used to work | The free project was paused for inactivity | Restore it from the Supabase dashboard and wait until it shows as active |
| `password authentication failed` | The password in the URI is wrong or isn't URL-encoded | URL-encode any special characters, or reset the password (step 2f) |
| Sign-in fails with `ATLAS_SESSION_SECRET is not set` | The secret is missing from `api/.env` | Generate one (step 3) and restart the API |
| "Wrong email or password" for a seeded user | The seed hasn't run, or it ran with a different `ATLAS_SEED_PASSWORD` | Run `uv run atlas-seed` again |
| `429 Too many attempts` | 8 failed sign-ins for that address | Wait 5 minutes, or restart the API (the counter is kept in memory) |
| Chat or search returns a 503 that mentions a provider | The model id in `ATLAS_GENERATION_MODEL` or `ATLAS_EMBEDDING_MODEL` belongs to a provider whose key isn't set | Set that provider's key, or switch back to a Gemini model id |
| Documents stay `queued` | The worker isn't running | Start `uv run atlas-worker` and check `/health/queue` |
| An upload works but nobody except admin can find it | The document has no groups and isn't public | Set groups on the upload or source, or mark it company-wide |
| Search breaks after you change the embedding model | `ATLAS_EMBEDDING_DIM` must equal the `vector(768)` column | Keep the dimension at 768, and bump `ATLAS_EMBEDDING_VERSION` so the reindex job re-embeds existing documents |
