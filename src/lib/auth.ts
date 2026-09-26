"use client";

/**
 * Who is signed in, in one place.
 *
 * A module-level store rather than a context provider, for the same reason
 * `chats.ts` is one: the sidebar, the sign-in page and every route need this,
 * and they are siblings rather than a tree with a convenient common parent.
 *
 * Nothing here holds a token. The session is an httpOnly cookie the browser
 * keeps and JavaScript cannot read; this caches only the server's answer when
 * asked who that cookie belongs to. It is deliberately not persisted — a
 * revoked or expired cookie must not be papered over by stale local state.
 */

import { ApiError, api, type AuthUser } from "@/lib/api";

export type AuthStatus = "loading" | "in" | "out";

export interface AuthState {
  status: AuthStatus;
  user: AuthUser | null;
  /** The API could not be reached at all — distinct from "signed out". */
  offline: boolean;
}

const LOADING: AuthState = { status: "loading", user: null, offline: false };

let state: AuthState = LOADING;
let inflight: Promise<void> | null = null;

const listeners = new Set<() => void>();

function set(next: AuthState) {
  state = next;
  for (const fn of listeners) fn();
}

export const auth = {
  subscribe(fn: () => void): () => void {
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  },

  snapshot(): AuthState {
    return state;
  },

  /**
   * Asks the server who we are. Deduplicated: several components mounting at
   * once must not each fire a request.
   */
  refresh(): Promise<void> {
    if (inflight) return inflight;
    inflight = api
      .me()
      .then((user) => {
        set({ status: "in", user, offline: false });
      })
      .catch((err: unknown) => {
        // 401 is the ordinary signed-out answer. A network failure is not —
        // conflating them would tell someone their password is wrong when the
        // backend is simply not running.
        const offline = err instanceof ApiError && err.status === 0;
        set({ status: "out", user: null, offline });
      })
      .finally(() => {
        inflight = null;
      });
    return inflight;
  },

  /** After a successful sign-in or sign-up, skipping the round trip. */
  adopt(user: AuthUser) {
    set({ status: "in", user, offline: false });
  },

  async signOut(): Promise<void> {
    try {
      await api.logout();
    } catch {
      // The cookie may already be gone, or the API may be down. Either way the
      // local answer is the same, and refusing to sign out because the network
      // failed is the wrong end of that trade.
    }
    set({ status: "out", user: null, offline: false });
  },
};

/** Server render and first paint see this; the client fills it in. */
export const AUTH_LOADING = LOADING;

/** Routes that render without a session. Everything else redirects. */
const PUBLIC_ROUTES = new Set(["/", "/signin"]);

export function isPublicRoute(pathname: string): boolean {
  return PUBLIC_ROUTES.has(pathname);
}
