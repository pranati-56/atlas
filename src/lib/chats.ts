"use client";

/**
 * Chat sessions, kept in localStorage.
 *
 * The sidebar and the /ask page are siblings, not parent and child, so the list
 * lives in a module-level store both subscribe to rather than in either one's
 * state. There is no server-side chat table yet; when there is, only the
 * functions on `chats` below change.
 *
 * Retrieval traces are deliberately not persisted — a single trace is tens of
 * kilobytes of passage text and would exhaust the origin quota after a handful
 * of conversations. The trace drawer therefore only opens for answers produced
 * in the current page session, which is where anyone actually wants it.
 */

import type { Citation } from "@/lib/api";

export interface StoredTurn {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  note?: string | null;
  /** Summary of the trace, kept so restored answers still show a meta row. */
  passages?: number;
  ms?: number;
  cost?: number;
}

export interface ChatSession {
  id: string;
  title: string;
  updated: number;
  turns: StoredTurn[];
}

const KEY = "atlas.chats.v1";
const ACTIVE = "atlas.chats.active";
const LIMIT = 40;

let sessions: ChatSession[] = [];
let activeId: string | null = null;
let loaded = false;

const listeners = new Set<() => void>();

function emit() {
  for (const fn of listeners) fn();
}

function load() {
  if (loaded || typeof window === "undefined") return;
  loaded = true;
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw) sessions = JSON.parse(raw) as ChatSession[];
    activeId = window.localStorage.getItem(ACTIVE);
  } catch {
    // A corrupt store must not take the page down with it.
    sessions = [];
    activeId = null;
  }
  if (activeId && !sessions.some((s) => s.id === activeId)) activeId = null;
}

function persist() {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(sessions.slice(0, LIMIT)));
    if (activeId) window.localStorage.setItem(ACTIVE, activeId);
    else window.localStorage.removeItem(ACTIVE);
  } catch {
    /* quota — the in-memory list still works for this page session */
  }
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

/** First question, trimmed to something that fits the rail. */
function titleFrom(turns: StoredTurn[]): string {
  const first = turns.find((t) => t.role === "user")?.content.trim();
  if (!first) return "New chat";
  const oneLine = first.replace(/\s+/g, " ");
  return oneLine.length > 44 ? `${oneLine.slice(0, 43)}…` : oneLine;
}

/* ──────────────────────────────────────────────────────────────── store ── */

export const chats = {
  subscribe(fn: () => void): () => void {
    load();
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  },

  /** Identity is stable between emits, which is what useSyncExternalStore wants. */
  snapshot(): ChatSession[] {
    load();
    return sessions;
  },

  active(): string | null {
    load();
    return activeId;
  },

  get(id: string | null): ChatSession | null {
    load();
    return sessions.find((s) => s.id === id) ?? null;
  },

  /** Starts a blank conversation. Nothing is written until the first answer. */
  start() {
    load();
    activeId = null;
    persist();
    emit();
  },

  select(id: string) {
    load();
    activeId = id;
    persist();
    emit();
  },

  /**
   * Writes the current transcript, creating the session on first save.
   * Returns the id so the caller keeps writing to the same row.
   */
  save(id: string | null, turns: StoredTurn[]): string {
    load();
    if (turns.length === 0) return id ?? "";
    const now = Date.now();
    const existing = id ? sessions.find((s) => s.id === id) : null;

    if (existing) {
      const updated: ChatSession = {
        ...existing,
        turns,
        updated: now,
        title: existing.title === "New chat" ? titleFrom(turns) : existing.title,
      };
      sessions = [updated, ...sessions.filter((s) => s.id !== existing.id)];
    } else {
      const session: ChatSession = {
        id: id || newId(),
        title: titleFrom(turns),
        updated: now,
        turns,
      };
      sessions = [session, ...sessions];
      activeId = session.id;
      id = session.id;
    }

    sessions = sessions.slice(0, LIMIT);
    persist();
    emit();
    return id ?? "";
  },

  remove(id: string) {
    load();
    sessions = sessions.filter((s) => s.id !== id);
    if (activeId === id) activeId = null;
    persist();
    emit();
  },
};

/** Server render sees an empty list; the client fills it on hydration. */
export const EMPTY: ChatSession[] = [];

export function relativeTime(ts: number): string {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return d < 7 ? `${d}d ago` : new Date(ts).toLocaleDateString();
}
