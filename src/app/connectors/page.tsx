"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { SOURCES, sourceMeta, type SourceKind } from "@/components/icons";
import { ApiError, api, type SourceRow } from "@/lib/api";

/* ─────────────────────────────────────────────────────────── connect specs ── */

interface FieldSpec {
  key: string;
  label: string;
  placeholder?: string;
  note?: string;
  secret?: boolean;
  optional?: boolean;
  /** Whitespace-separated input collected into an array (URLs, channels). */
  list?: boolean;
  multiline?: boolean;
}

interface ConnectSpec {
  kind: SourceKind;
  title: string;
  hint: string;
  /** What the source is called when no field supplies a name. */
  defaultName: string;
  /** Config key whose value makes a better name than the default. */
  nameFrom?: string;
  fields: FieldSpec[];
}

/**
 * One spec per connector, rather than one modal component per connector.
 *
 * Every one of these is the same shape — a few credentials, an optional scope,
 * then create-and-sync — and six near-identical dialogs would drift apart the
 * first time one of them gained a field.
 */
const SPECS: Record<string, ConnectSpec> = {
  github: {
    kind: "github",
    title: "Connect a GitHub repository",
    hint: "Atlas walks the tree once, then follows commits. Only changed paths are re-read.",
    defaultName: "GitHub",
    nameFrom: "repository",
    fields: [
      { key: "repository", label: "Repository", placeholder: "owner/name" },
      {
        key: "branch",
        label: "Branch",
        placeholder: "HEAD",
        optional: true,
        note: "Blank uses the default branch.",
      },
      {
        key: "token",
        label: "Access token",
        placeholder: "Optional for public repos",
        secret: true,
        optional: true,
        note: "A fine-grained PAT with Contents: read. Falls back to GITHUB_TOKEN.",
      },
    ],
  },
  slack: {
    kind: "slack",
    title: "Connect a Slack workspace",
    hint: "Threads are indexed whole — a reply is unreadable without the question above it.",
    defaultName: "Slack",
    fields: [
      {
        key: "token",
        label: "Bot token",
        placeholder: "xoxb-…",
        secret: true,
        note: "Scopes: channels:read, channels:history, users:read. Falls back to SLACK_BOT_TOKEN.",
      },
      {
        key: "channels",
        label: "Channels",
        placeholder: "security incidents platform",
        optional: true,
        list: true,
        note: "Names or IDs, space separated. Blank means every public channel the bot has joined.",
      },
    ],
  },
  notion: {
    kind: "notion",
    title: "Connect Notion",
    hint: "Only pages shared with the integration are visible — Notion's own sharing is the first permission filter.",
    defaultName: "Notion",
    fields: [
      {
        key: "token",
        label: "Integration token",
        placeholder: "ntn_…",
        secret: true,
        note: "Create an internal integration, then share the pages or teamspaces with it. Falls back to NOTION_TOKEN.",
      },
    ],
  },
  gdrive: {
    kind: "gdrive",
    title: "Connect Google Drive",
    hint: "Docs, Sheets and Slides are exported as text; PDFs and Word files are parsed as uploads are.",
    defaultName: "Google Drive",
    fields: [
      {
        key: "refresh_token",
        label: "OAuth refresh token",
        secret: true,
        note: "Issued for the drive.readonly scope.",
      },
      {
        key: "client_id",
        label: "Client ID",
        optional: true,
        note: "Blank falls back to GOOGLE_CLIENT_ID.",
      },
      {
        key: "client_secret",
        label: "Client secret",
        secret: true,
        optional: true,
        note: "Blank falls back to GOOGLE_CLIENT_SECRET.",
      },
      {
        key: "folder_id",
        label: "Folder",
        optional: true,
        note: "A folder ID to limit the sync. Blank indexes everything reachable.",
      },
    ],
  },
  jira: {
    kind: "jira",
    title: "Connect Jira",
    hint: "Each issue is indexed with its comments — the decision is usually in the fourth reply, not the description.",
    defaultName: "Jira",
    nameFrom: "project",
    fields: [
      {
        key: "base_url",
        label: "Site URL",
        placeholder: "https://acme.atlassian.net",
        optional: true,
        note: "Blank falls back to JIRA_BASE_URL.",
      },
      { key: "email", label: "Account email", optional: true },
      { key: "token", label: "API token", secret: true, optional: true },
      {
        key: "project",
        label: "Project key",
        placeholder: "PLAT",
        optional: true,
        note: "Blank indexes every project the account can see.",
      },
    ],
  },
  web: {
    kind: "web",
    title: "Add a URL list",
    hint: "A fixed list, not a crawler — every URL here is fetched on each sync and nothing else is.",
    defaultName: "Web",
    fields: [
      {
        key: "__name",
        label: "Name",
        placeholder: "Engineering handbook",
      },
      {
        key: "urls",
        label: "URLs",
        list: true,
        multiline: true,
        placeholder: "https://handbook.acme.com/engineering",
        note: "One per line.",
      },
    ],
  },
};

/** Order the cards read best in — the two people reach for first, first. */
const ADDABLE: SourceKind[] = ["slack", "notion", "gdrive", "github", "jira", "web"];

/** In the schema's enum but with no connector, or not in the enum at all. */
const PLANNED: SourceKind[] = ["linear", "confluence", "figma"];

export default function ConnectorsPage() {
  const [sources, setSources] = useState<SourceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<ConnectSpec | null>(null);
  const [syncing, setSyncing] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.sources();
      setSources(res.sources);
      setError(null);
    } catch (err) {
      setSources([]);
      setError(err instanceof ApiError ? err.message : "Could not load connectors.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll only while something is mid-sync. A settled list needs no heartbeat.
  useEffect(() => {
    const busy = sources?.some(
      (s) => s.status === "syncing" || s.status === "queued",
    );
    if (!busy) return;
    const id = setInterval(() => void load(), 2000);
    return () => clearInterval(id);
  }, [sources, load]);

  const sync = useCallback(
    async (id: string) => {
      setSyncing(id);
      try {
        await api.syncSource(id);
        await load();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Sync failed.");
      } finally {
        setSyncing(null);
      }
    },
    [load],
  );

  const connected = sources ?? [];

  return (
    <div className="min-h-screen">
      <div className="mx-auto w-full max-w-[62rem] px-8 pb-24 pt-14">
        <header className="max-w-xl">
          <h1 className="display text-[2.4rem] text-ink">
            Everything your company <span className="accent">already wrote</span>.
          </h1>
          <p className="mt-4 text-base leading-relaxed text-ink-dim">
            Point Atlas at a source and it keeps itself current — re-reading only
            what changed, and carrying each document&rsquo;s permissions through
            to retrieval.
          </p>
        </header>

        {error && (
          <div className="fade mt-7 rounded-[9px] border border-[rgba(224,115,109,0.28)] bg-[rgba(224,115,109,0.07)] px-3.5 py-2.5">
            <p className="text-xs leading-relaxed text-[#eda6a1]">{error}</p>
          </div>
        )}

        {connected.length > 0 && (
          <section className="mt-12">
            <div className="flex items-baseline justify-between border-b border-line pb-2.5">
              <span className="label">Connected</span>
              <span className="num text-2xs text-ink-ghost">{connected.length}</span>
            </div>
            <div className="mt-1 flex flex-col">
              {connected.map((s) => (
                <ConnectedRow
                  key={s.id}
                  source={s}
                  syncing={syncing === s.id}
                  onSync={() => void sync(s.id)}
                />
              ))}
            </div>
          </section>
        )}

        <section className="mt-14">
          <div className="border-b border-line pb-2.5">
            <span className="label">Add a source</span>
          </div>

          {/* Asymmetric by design. Slack is what most people come for and it is
              the connector with the most to explain, so it gets four columns
              and two rows; everything else is a two-column tile. */}
          <div className="mt-4 grid gap-3 md:grid-cols-6">
            {ADDABLE.map((kind) => (
              <AddCard
                key={kind}
                kind={kind}
                hero={kind === "slack"}
                onClick={() => setOpen(SPECS[kind] ?? null)}
              />
            ))}
          </div>

          <div className="mt-3 grid gap-3 md:grid-cols-6">
            <UploadCard />
          </div>
        </section>

        <section className="mt-12">
          <div className="flex items-baseline justify-between border-b border-line pb-2.5">
            <span className="label">Not yet</span>
            <span className="text-2xs text-ink-ghost">
              No sync code and no enum value — these are honest blanks.
            </span>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {PLANNED.map((kind) => {
              const meta = SOURCES[kind];
              return (
                <span
                  key={kind}
                  className="flex items-center gap-2 rounded-[8px] border border-line px-2.5 py-1.5 text-2xs text-ink-ghost"
                >
                  <meta.Glyph className="h-3.5 w-3.5" />
                  {meta.label}
                  <span className="chip chip-soon">Soon</span>
                </span>
              );
            })}
          </div>
        </section>
      </div>

      {open && (
        <ConnectModal
          spec={open}
          onClose={() => setOpen(null)}
          onDone={() => {
            setOpen(null);
            void load();
          }}
        />
      )}
    </div>
  );
}

/* ──────────────────────────────────────────────────────────── connected ── */

function ConnectedRow({
  source,
  syncing,
  onSync,
}: {
  source: SourceRow;
  syncing: boolean;
  onSync: () => void;
}) {
  const meta = sourceMeta(source.kind);
  const live = source.status === "syncing" || source.status === "queued";
  const failed = source.status === "error";

  return (
    <article className="flex items-center gap-3.5 border-b border-line py-3 last:border-b-0">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[8px] border border-line-strong bg-s2 text-ink-dim shadow-[inset_0_1px_0_rgba(255,250,240,0.05)]">
        <meta.Glyph className="h-4 w-4" />
      </span>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm text-ink">{source.name}</span>
          {source.has_token && <span className="chip chip-muted">token set</span>}
          {source.default_acl_public ? (
            <span className="chip chip-muted">everyone</span>
          ) : source.default_acl_groups.length > 0 ? (
            <span className="chip chip-muted">
              {source.default_acl_groups.join(", ")}
            </span>
          ) : (
            <span className="chip chip-muted">admins only</span>
          )}
        </div>
        <p className="mt-0.5 truncate text-2xs text-ink-ghost">
          {failed
            ? (source.last_error ?? "Last sync failed")
            : [
                `${source.document_count.toLocaleString()} ${
                  source.document_count === 1 ? "document" : "documents"
                }`,
                source.last_sync_at
                  ? `synced ${new Date(source.last_sync_at).toLocaleString()}`
                  : "never synced",
              ].join(" · ")}
        </p>
      </div>

      <StatusChip status={source.status} />

      <button
        type="button"
        onClick={onSync}
        disabled={syncing || live}
        className="btn h-7 shrink-0 px-2.5"
      >
        {live ? "Syncing…" : syncing ? "Queuing…" : "Sync now"}
      </button>
    </article>
  );
}

function StatusChip({ status }: { status: string }) {
  if (status === "error")
    return (
      <span className="chip shrink-0 border-[rgba(224,115,109,0.3)] bg-[rgba(224,115,109,0.1)] text-[#eda6a1]">
        error
      </span>
    );
  if (status === "syncing" || status === "queued")
    return <span className="chip chip-dense shrink-0">{status}</span>;
  return <span className="chip chip-live shrink-0">ready</span>;
}

/* ─────────────────────────────────────────────────────────────── adding ── */

function AddCard({
  kind,
  hero,
  onClick,
}: {
  kind: SourceKind;
  hero?: boolean;
  onClick: () => void;
}) {
  const meta = SOURCES[kind];
  const [hover, setHover] = useState(false);

  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className={`surface surface-hover relative flex flex-col overflow-hidden p-4 text-left ${
        hero ? "md:col-span-4 md:row-span-2" : "md:col-span-2"
      }`}
    >
      {/* Brand colour appears here and nowhere else: a soft radial behind the
          tile, only while the pointer is on the card. */}
      <span
        className="pointer-events-none absolute -left-10 -top-10 h-48 w-48 rounded-full transition-opacity duration-300"
        style={{
          background: `radial-gradient(50% 50% at 50% 50%, ${meta.glow}, transparent 70%)`,
          opacity: hover ? 0.14 : 0,
        }}
      />

      <div className="relative flex items-center gap-2.5">
        <span className="flex h-9 w-9 items-center justify-center rounded-[9px] border border-line-strong bg-s2 text-ink shadow-[inset_0_1px_0_rgba(255,250,240,0.06)]">
          <meta.Glyph className="h-[18px] w-[18px]" />
        </span>
        <span className="text-sm font-medium text-ink">{meta.label}</span>
        <span className="ml-auto text-2xs text-ink-ghost">Connect →</span>
      </div>

      <p className="relative mt-3 text-xs leading-relaxed text-ink-faint">
        {meta.blurb}
      </p>

      {hero && <ThreadPreview />}
    </button>
  );
}

/**
 * Why Slack is indexed by thread, shown rather than asserted.
 *
 * The last line is the whole argument: on its own it is meaningless, and under
 * the two above it, it is the answer.
 */
function ThreadPreview() {
  return (
    <div className="relative mt-4 flex-1 rounded-[10px] border border-line bg-[rgba(0,0,0,0.32)] p-3">
      <div className="flex items-center gap-1.5 pb-2">
        <span className="label">Indexed as one passage</span>
        <span className="chip chip-muted ml-auto">3 messages</span>
      </div>
      <div className="flex flex-col gap-1.5 text-2xs leading-relaxed">
        <p className="text-ink-dim">
          <span className="text-ink-faint">priya</span> · Do we cap SSO sessions
          at 8 or 12 hours in prod?
        </p>
        <p className="text-ink-dim">
          <span className="text-ink-faint">daniel</span> · 12. We raised it after
          the March incident review.
        </p>
        <p className="text-ink">
          <span className="text-ink-faint">priya</span> · Confirmed — shipping the
          config change today.
        </p>
      </div>
    </div>
  );
}

function UploadCard() {
  const meta = SOURCES.upload;
  return (
    <Link
      href="/corpus"
      className="surface surface-hover col-span-6 flex items-center gap-3 p-3.5"
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[8px] border border-line-strong bg-s2 text-ink-dim">
        <meta.Glyph className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-sm text-ink">Or just drop files in</span>
        <span className="block text-2xs text-ink-ghost">{meta.blurb}</span>
      </span>
      <span className="btn h-7 px-2.5">Go to Corpus</span>
    </Link>
  );
}

/* ──────────────────────────────────────────────────────────────── modal ── */

function ConnectModal({
  spec,
  onClose,
  onDone,
}: {
  spec: ConnectSpec;
  onClose: () => void;
  onDone: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [audience, setAudience] = useState<"everyone" | "groups">("everyone");
  const [groups, setGroups] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const missing = spec.fields.filter(
    (f) => !f.optional && !(values[f.key] ?? "").trim(),
  );
  const canSubmit =
    missing.length === 0 && (audience === "everyone" || groups.trim().length > 0);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const config: Record<string, unknown> = {};
      for (const field of spec.fields) {
        const raw = (values[field.key] ?? "").trim();
        if (!raw || field.key === "__name") continue;
        config[field.key] = field.list ? raw.split(/\s+/).filter(Boolean) : raw;
      }

      const name =
        (values.__name ?? "").trim() ||
        (spec.nameFrom ? (values[spec.nameFrom] ?? "").trim() : "") ||
        spec.defaultName;

      const source = await api.createSource({
        kind: spec.kind,
        name,
        config,
        acl_public: audience === "everyone",
        acl_groups:
          audience === "groups"
            ? groups
                .split(/[,\s]+/)
                .map((g) => g.trim())
                .filter(Boolean)
            : [],
      });
      // Creating without syncing leaves an empty source that looks broken.
      await api.syncSource(source.id).catch(() => {});
      onDone();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not create the source.",
      );
    } finally {
      setBusy(false);
    }
  };

  const meta = SOURCES[spec.kind];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute inset-0 bg-[rgba(0,0,0,0.55)] backdrop-blur-[2px]"
      />
      <div className="border-lit fade relative max-h-[86vh] w-full max-w-[27rem] overflow-y-auto rounded-[14px] bg-s3 p-5 shadow-[0_28px_70px_rgba(0,0,0,0.6)]">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-[8px] border border-line-strong bg-s2 text-ink">
            <meta.Glyph className="h-4 w-4" />
          </span>
          <h2 className="text-sm font-medium text-ink">{spec.title}</h2>
        </div>
        <p className="mt-2 text-2xs leading-relaxed text-ink-ghost">{spec.hint}</p>

        <div className="mt-4 flex flex-col gap-3">
          {spec.fields.map((field) => (
            <label key={field.key} className="flex flex-col gap-1.5">
              <span className="flex items-baseline gap-1.5 text-2xs text-ink-faint">
                {field.label}
                {field.optional && (
                  <span className="text-ink-ghost">optional</span>
                )}
              </span>
              {field.multiline ? (
                <textarea
                  rows={4}
                  value={values[field.key] ?? ""}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, [field.key]: e.target.value }))
                  }
                  placeholder={field.placeholder}
                  className="field resize-none font-mono text-2xs leading-relaxed"
                />
              ) : (
                <input
                  type={field.secret ? "password" : "text"}
                  value={values[field.key] ?? ""}
                  onChange={(e) =>
                    setValues((v) => ({ ...v, [field.key]: e.target.value }))
                  }
                  placeholder={field.placeholder}
                  className="field"
                />
              )}
              {field.note && (
                <span className="text-2xs leading-relaxed text-ink-ghost">
                  {field.note}
                </span>
              )}
            </label>
          ))}

          <Audience
            value={audience}
            onChange={setAudience}
            groups={groups}
            onGroups={setGroups}
          />
        </div>

        {error && (
          <p className="mt-3 text-2xs leading-relaxed text-[#eda6a1]">{error}</p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="btn">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!canSubmit || busy}
            className="btn btn-primary"
          >
            {busy ? "Connecting…" : "Connect and sync"}
          </button>
        </div>

        <p className="mt-3 text-2xs leading-relaxed text-ink-ghost">
          Credentials are stored server-side and never sent back to this page.
        </p>
      </div>
    </div>
  );
}

/**
 * Who can retrieve from this source.
 *
 * Not a nicety: a source created with no groups and not public is reachable by
 * admins alone, which looks exactly like a broken sync. Making the choice
 * explicit at connect time is the only way that stays honest.
 */
function Audience({
  value,
  onChange,
  groups,
  onGroups,
}: {
  value: "everyone" | "groups";
  onChange: (v: "everyone" | "groups") => void;
  groups: string;
  onGroups: (v: string) => void;
}) {
  return (
    <div className="mt-1 flex flex-col gap-2 border-t border-line pt-3">
      <span className="text-2xs text-ink-faint">Who can search this?</span>
      <div className="flex gap-1.5">
        {(["everyone", "groups"] as const).map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => onChange(option)}
            className={`rounded-[6px] border px-2.5 py-1 text-2xs transition-colors ${
              value === option
                ? "border-line-lit bg-s2 text-ink"
                : "border-line text-ink-faint hover:text-ink-dim"
            }`}
          >
            {option === "everyone" ? "Everyone" : "Specific groups"}
          </button>
        ))}
      </div>
      {value === "groups" && (
        <input
          value={groups}
          onChange={(e) => onGroups(e.target.value)}
          placeholder="engineering, security"
          className="field"
        />
      )}
    </div>
  );
}
