/**
 * Typed client for the FastAPI service.
 *
 * Shapes mirror the server's response models exactly. Where the server sends
 * snake_case it stays snake_case — renaming at the boundary buys nothing and
 * makes every field a lookup between two vocabularies.
 */

export const API =
  process.env.NEXT_PUBLIC_ATLAS_API?.replace(/\/$/, "") ?? "http://localhost:8000";

/* ─────────────────────────────────────────────────────────────────── types ── */

export interface RetrievalParams {
  alpha: number;
  top_k: number;
  candidates: number;
  rrf_k: number;
  min_score: number;
  rerank: boolean;
  rewrite_query: boolean;
}

export const DEFAULT_PARAMS: RetrievalParams = {
  alpha: 0.5,
  top_k: 8,
  candidates: 60,
  rrf_k: 60,
  min_score: 0,
  rerank: false,
  rewrite_query: true,
};

export interface RetrievedChunk {
  chunk_id: string;
  document_id: string;
  document_title: string;
  document_uri: string | null;
  source_id: string;
  source_kind: string;
  external_id: string;
  ordinal: number;
  page: number | null;
  heading: string | null;
  content: string;
  metadata: Record<string, unknown>;
  /** Cosine similarity, 0..1. null when only the lexical channel found it. */
  dense_score: number | null;
  /** BM25, unbounded above. null when only the vector channel found it. */
  sparse_score: number | null;
  /** Normalised 0..1 — the ordering key. */
  fused_score: number;
  dense_rank: number | null;
  sparse_rank: number | null;
  rerank_score: number | null;
}

export interface Timings {
  embed_ms: number;
  dense_ms: number;
  sparse_ms: number;
  fuse_ms: number;
  rerank_ms: number | null;
  generate_ms: number | null;
  total_ms: number;
}

export interface RetrievalTrace {
  query: string;
  rewritten_query: string | null;
  params: RetrievalParams;
  dense: RetrievedChunk[];
  sparse: RetrievedChunk[];
  fused: RetrievedChunk[];
  timings: Timings;
  context_tokens: number;
}

export interface Citation {
  marker: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  document_uri: string | null;
  source_kind: string;
  page: number | null;
  snippet: string;
}

export type Stage =
  | "queued"
  | "fetching"
  | "extracting"
  | "chunking"
  | "embedding"
  | "indexing"
  | "ready"
  | "failed"
  | "skipped";

export interface DocumentRow {
  id: string;
  title: string;
  kind: string;
  stage: Stage;
  progress: number;
  chunk_count: number;
  token_count: number;
  size_bytes: number;
  error: string | null;
  source_kind: string;
  source_name: string;
  created_at: string;
}

export interface CorpusCounts {
  documents: number;
  chunks: number;
  ready: number;
  failed: number;
}

/**
 * A row of `sources`, as the API hands it back.
 *
 * `config` arrives with every credential-shaped key stripped server-side.
 * `credentials` names which ones are set — the UI can say "token set" without
 * a secret ever reaching the browser.
 */
export interface SourceRow {
  id: string;
  kind: string;
  name: string;
  config: Record<string, unknown>;
  credentials: string[];
  has_token: boolean;
  cursor: Record<string, unknown>;
  default_acl_groups: string[];
  default_acl_public: boolean;
  status: "idle" | "queued" | "syncing" | "error" | string;
  last_sync_at: string | null;
  last_error: string | null;
  document_count: number;
  created_at: string;
}

export interface AuthUser {
  email: string;
  tenant: string;
  tenant_name: string | null;
  is_admin: boolean;
  groups: string[];
}

/** What the sign-in page needs before anyone has signed in. */
export interface AuthConfig {
  allow_signup: boolean;
  /** Non-null when ATLAS_DEV_USER is set — the password form is not guarding anything. */
  dev_user: string | null;
  min_password: number;
}

/** One address can hold accounts in several workspaces; the caller picks. */
export interface WorkspaceChoice {
  slug: string;
  name: string;
}

export type LoginResult =
  | { ok: true; user: AuthUser }
  | { ok: false; reason: "ambiguous"; choices: WorkspaceChoice[] };

export interface HealthReport {
  ok: boolean;
  config: { ok: boolean; error?: string; providers?: Record<string, boolean> };
  database: { ok: boolean; error?: string };
  extensions: Record<string, boolean>;
  migrations_applied: string[];
  models: { generation: string[]; embedding: string[] };
  connectors: string[];
}

/* ──────────────────────────────────────────────────────────────── requests ── */

export class ApiError extends Error {
  readonly status: number;
  readonly kind?: string;
  constructor(status: number, message: string, kind?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, {
      // The session is a cookie on a different origin in development, so every
      // request has to opt in explicitly.
      credentials: "include",
      ...init,
      headers: {
        ...(init.body instanceof FormData
          ? {}
          : { "content-type": "application/json" }),
        ...init.headers,
      },
    });
  } catch {
    // A dead backend is the single most common local failure. Say so, rather
    // than surfacing "Failed to fetch".
    throw new ApiError(
      0,
      `Cannot reach the Atlas API at ${API}. Start it with: cd api && uv run uvicorn atlas.main:app --reload`,
      "network",
    );
  }

  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as {
      detail?: string;
      kind?: string;
    } | null;
    throw new ApiError(
      res.status,
      body?.detail ?? `${res.status} ${res.statusText}`,
      body?.kind,
    );
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export const api = {
  health: () => request<HealthReport>("/health"),

  documents: (sourceId?: string) =>
    request<{ documents: DocumentRow[]; counts: CorpusCounts }>(
      `/documents${sourceId ? `?source_id=${sourceId}` : ""}`,
    ),

  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ document_id: string; stage: string }>("/documents", {
      method: "POST",
      body: form,
    });
  },

  deleteDocument: (id: string) =>
    request<void>(`/documents/${id}`, { method: "DELETE" }),

  authConfig: () => request<AuthConfig>("/auth/config"),

  /** 401 here is the normal signed-out answer, not a failure. */
  me: () => request<AuthUser>("/auth/me"),

  login: (body: { email: string; password: string; workspace?: string }) =>
    request<LoginResult>("/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  signup: (body: {
    workspace: string;
    email: string;
    password: string;
    name?: string;
  }) =>
    request<{ ok: true; user: AuthUser }>("/auth/signup", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  logout: () => request<void>("/auth/logout", { method: "POST" }),

  sources: () =>
    request<{ sources: SourceRow[]; kinds: string[] }>("/sources"),

  createSource: (body: {
    kind: string;
    name: string;
    config: Record<string, unknown>;
    acl_groups?: string[];
    acl_public?: boolean;
  }) =>
    request<SourceRow>("/sources", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  syncSource: (id: string) =>
    request<{ queued: boolean; job_id: string | null; already_pending: boolean }>(
      `/sources/${id}/sync`,
      { method: "POST" },
    ),

  search: (query: string, params: RetrievalParams) =>
    request<{ trace: RetrievalTrace }>("/search", {
      method: "POST",
      body: JSON.stringify({ query, params }),
    }),
};

/* ────────────────────────────────────────────────────────────────── stream ── */

export type ChatFrame =
  | { type: "trace"; trace: RetrievalTrace }
  | { type: "delta"; text: string }
  | {
      type: "done";
      content: string;
      citations: Citation[];
      generate_ms: number;
      stop_reason?: string;
      cost_usd?: number;
      error?: string | null;
    }
  | { type: "error"; error: string };

export interface ChatOptions {
  query: string;
  history: { role: string; content: string }[];
  params: RetrievalParams;
  model?: string;
  signal?: AbortSignal;
}

/**
 * Reads the NDJSON chat stream, yielding one frame per line.
 *
 * A network chunk boundary can land mid-line, so the tail is held back until a
 * newline arrives. Parsing eagerly would corrupt roughly one frame per response
 * and the failure would look like a model problem.
 */
export async function* chatStream(
  opts: ChatOptions,
): AsyncGenerator<ChatFrame, void, void> {
  const res = await fetch(`${API}/chat`, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    signal: opts.signal,
    body: JSON.stringify({
      query: opts.query,
      history: opts.history,
      params: opts.params,
      ...(opts.model ? { model: opts.model } : {}),
    }),
  });

  if (!res.ok || !res.body) {
    const body = (await res.json().catch(() => null)) as {
      detail?: string;
      kind?: string;
    } | null;
    throw new ApiError(res.status, body?.detail ?? "Chat request failed", body?.kind);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() ?? ""; // incomplete tail, held for the next chunk

      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          yield JSON.parse(line) as ChatFrame;
        } catch {
          // A single malformed frame is not worth killing the answer over.
          console.warn("[atlas] unparseable stream frame", line);
        }
      }
    }
    if (buffer.trim()) {
      try {
        yield JSON.parse(buffer) as ChatFrame;
      } catch {
        /* truncated final line — the stream was cut */
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/* ──────────────────────────────────────────────────────────────── helpers ── */

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(2)}s`;
}

/**
 * Where a chunk sits between the two channels: 0 = purely lexical, 1 = purely
 * dense. Drives the mix point of every fusion bar, which is what makes the
 * colour language readable at a glance.
 */
export function channelBias(chunk: RetrievedChunk): number {
  const d = chunk.dense_rank;
  const s = chunk.sparse_rank;
  if (d != null && s == null) return 1;
  if (s != null && d == null) return 0;
  if (d == null || s == null) return 0.5;
  // Reciprocal rank, so rank 1 dominates rank 10 the way RRF itself weights it.
  const dw = 1 / (60 + d);
  const sw = 1 / (60 + s);
  return dw / (dw + sw);
}
