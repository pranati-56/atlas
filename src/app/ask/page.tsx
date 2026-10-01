"use client";

import Link from "next/link";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { Inspector } from "@/components/Inspector";
import { SOURCES, sourceMeta, type SourceKind } from "@/components/icons";
import {
  ApiError,
  DEFAULT_PARAMS,
  api,
  chatStream,
  formatMs,
  type Citation,
  type CorpusCounts,
  type RetrievalTrace,
  type SourceRow,
} from "@/lib/api";
import { EMPTY, chats, type StoredTurn } from "@/lib/chats";

interface Turn extends StoredTurn {
  /** Live only. Traces are not persisted — see lib/chats.ts. */
  trace?: RetrievalTrace | null;
}

const SUGGESTIONS = [
  "What did we decide about SSO session length?",
  "Which services still call the deprecated /v1/auth endpoint?",
  "Why did checkout failures spike after v4.2?",
];

export default function AskPage() {
  const activeId = useSyncExternalStore(chats.subscribe, chats.active, () => null);
  const sessions = useSyncExternalStore(
    chats.subscribe,
    chats.snapshot,
    () => EMPTY,
  );

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<RetrievalTrace | null>(null);

  const abort = useRef<AbortController | null>(null);
  const tail = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);

  // The rail owns which conversation is open. When it changes under us — a new
  // chat, a history click — swap the transcript in. `sessions` is in the deps
  // so a restored transcript re-reads once the store hydrates on first paint.
  useEffect(() => {
    if (activeId === sessionId) return;
    setSessionId(activeId);
    setTurns(chats.get(activeId)?.turns ?? []);
    setDrawer(null);
    setError(null);
  }, [activeId, sessionId, sessions]);

  // Follow the stream, but only while the reader is already at the bottom.
  // Yanking the viewport away from someone reading an earlier answer is worse
  // than not following at all.
  useEffect(() => {
    const el = tail.current;
    const box = scroller.current;
    if (!el || !box) return;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 240;
    if (nearBottom) el.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  const ask = useCallback(
    async (question: string) => {
      const q = question.trim();
      if (!q || busy) return;

      setError(null);
      setDraft("");
      setBusy(true);

      const history = turns.map((t) => ({ role: t.role, content: t.content }));
      setTurns((prev) => [
        ...prev,
        { role: "user", content: q },
        { role: "assistant", content: "" },
      ]);

      const controller = new AbortController();
      abort.current = controller;

      const patch = (fn: (t: Turn) => Turn) =>
        setTurns((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last) next[next.length - 1] = fn(last);
          return next;
        });

      try {
        for await (const frame of chatStream({
          query: q,
          history,
          params: DEFAULT_PARAMS,
          signal: controller.signal,
        })) {
          if (frame.type === "trace") {
            patch((t) => ({
              ...t,
              trace: frame.trace,
              passages: frame.trace.fused.length,
              ms: frame.trace.timings.total_ms,
            }));
          } else if (frame.type === "delta") {
            patch((t) => ({ ...t, content: t.content + frame.text }));
          } else if (frame.type === "done") {
            patch((t) => ({
              ...t,
              content: frame.content || t.content,
              citations: frame.citations,
              note: frame.error ?? null,
              cost: frame.cost_usd,
              ms: t.trace
                ? t.trace.timings.total_ms + (frame.generate_ms ?? 0)
                : frame.generate_ms,
            }));
          } else {
            setError(frame.error);
          }
        }
      } catch (err) {
        if (controller.signal.aborted) {
          patch((t) => ({ ...t, note: "Stopped." }));
        } else {
          setError(err instanceof ApiError ? err.message : "Something went wrong.");
          // Drop the empty assistant turn rather than leaving a blank answer
          // where one never arrived.
          setTurns((prev) =>
            prev[prev.length - 1]?.content === "" ? prev.slice(0, -1) : prev,
          );
        }
      } finally {
        abort.current = null;
        setBusy(false);
        // Persist from the settled state, so a stopped or failed turn is stored
        // exactly as the reader last saw it.
        setTurns((prev) => {
          const id = chats.save(
            sessionId,
            prev.map(({ trace: _trace, ...rest }) => rest),
          );
          if (id) setSessionId(id);
          return prev;
        });
      }
    },
    [busy, turns, sessionId],
  );

  // T opens the last trace. Guarded so it does not fire inside the composer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      const typing =
        el?.tagName === "TEXTAREA" ||
        el?.tagName === "INPUT" ||
        el?.isContentEditable === true;
      if (typing) return;
      if (e.key === "t" || e.key === "T") {
        e.preventDefault();
        setDrawer((open) => {
          if (open) return null;
          for (let i = turns.length - 1; i >= 0; i--) {
            const t = turns[i]?.trace;
            if (t) return t;
          }
          return null;
        });
      }
      if (e.key === "Escape") setDrawer(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [turns]);

  const empty = turns.length === 0;

  return (
    <div className="relative flex h-screen flex-col">
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[45rem] px-6 pb-44 pt-14">
          {empty ? (
            <Welcome onPick={(q) => void ask(q)} />
          ) : (
            <div className="flex flex-col gap-9">
              {turns.map((turn, i) => (
                <TurnView
                  key={i}
                  turn={turn}
                  streaming={busy && i === turns.length - 1}
                  onTrace={setDrawer}
                />
              ))}
            </div>
          )}

          {error && (
            <div className="fade mt-7 rounded-[9px] border border-[rgba(224,115,109,0.28)] bg-[rgba(224,115,109,0.07)] px-3.5 py-2.5">
              <p className="text-xs leading-relaxed text-[#eda6a1]">{error}</p>
            </div>
          )}
          <div ref={tail} />
        </div>
      </div>

      <Composer
        value={draft}
        onChange={setDraft}
        onSubmit={() => void ask(draft)}
        onStop={() => abort.current?.abort()}
        busy={busy}
      />

      <TraceDrawer trace={drawer} onClose={() => setDrawer(null)} />
    </div>
  );
}

/* ────────────────────────────────────────────────────────────── welcome ── */

function Welcome({ onPick }: { onPick: (q: string) => void }) {
  const [counts, setCounts] = useState<CorpusCounts | null>(null);
  const [sources, setSources] = useState<SourceRow[] | null>(null);

  useEffect(() => {
    let alive = true;
    void api
      .documents()
      .then((r) => {
        if (alive) setCounts(r.counts);
      })
      .catch(() => {});
    void api
      .sources()
      .then((r) => {
        if (alive) setSources(r.sources);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const scope =
    counts && sources && sources.length > 0
      ? `Ask across ${sources.length} ${
          sources.length === 1 ? "source" : "sources"
        } · ${counts.chunks.toLocaleString()} indexed passages`
      : "Nothing connected yet";

  return (
    <div className="pt-6">
      <p className="rise label">{scope}</p>
      <h1 className="rise d1 display mt-4 text-[2.6rem] text-ink">
        Ask your company <span className="accent">anything</span>.
      </h1>
      <p className="rise d2 mt-4 max-w-lg text-base leading-relaxed text-ink-dim">
        Every answer carries the passages it came from, and only reaches
        documents you are allowed to see.
      </p>

      <div className="rise d3 mt-9 flex flex-col gap-1.5">
        {SUGGESTIONS.map((q) => (
          <button
            key={q}
            type="button"
            onClick={() => onPick(q)}
            className="surface surface-hover group flex items-center gap-3 px-3.5 py-3 text-left text-sm text-ink-dim"
          >
            <span className="h-1 w-1 shrink-0 rounded-full bg-ink-ghost transition-colors duration-150 group-hover:bg-ink-dim" />
            <span className="group-hover:text-ink">{q}</span>
            <span className="ml-auto shrink-0 text-2xs text-ink-ghost opacity-0 transition-opacity duration-150 group-hover:opacity-100">
              ↵
            </span>
          </button>
        ))}
      </div>

      {sources != null && <ConnectedRow sources={sources} />}
    </div>
  );
}

function ConnectedRow({ sources }: { sources: SourceRow[] }) {
  const connected = new Set(sources.map((s) => s.kind));
  const shown: SourceKind[] = ["gdrive", "notion", "slack", "github", "upload"];

  return (
    <div className="mt-10 border-t border-line pt-5">
      <div className="flex flex-wrap items-center gap-2">
        {shown.map((kind) => {
          const meta = SOURCES[kind];
          const on = connected.has(kind);
          return (
            <span
              key={kind}
              className={`flex items-center gap-1.5 rounded-[6px] border px-2 py-1 text-2xs ${
                on
                  ? "border-line-strong bg-s2 text-ink-dim"
                  : "border-line text-ink-ghost"
              }`}
              title={meta.blurb}
            >
              <meta.Glyph className="h-3.5 w-3.5" />
              {meta.label}
              {!meta.live && <span className="chip chip-soon ml-0.5">Soon</span>}
            </span>
          );
        })}
        <Link
          href="/connectors"
          className="text-2xs text-ink-faint underline decoration-line-strong underline-offset-4 transition-colors hover:text-ink"
        >
          Manage connectors
        </Link>
      </div>
    </div>
  );
}

/* ───────────────────────────────────────────────────────────────── turn ── */

function TurnView({
  turn,
  streaming,
  onTrace,
}: {
  turn: Turn;
  streaming: boolean;
  onTrace: (t: RetrievalTrace | null) => void;
}) {
  if (turn.role === "user") {
    return (
      <div className="rise flex justify-end">
        <p className="max-w-[82%] rounded-[12px] rounded-br-[4px] border border-line-strong bg-s2 px-3.5 py-2.5 text-sm shadow-[inset_0_1px_0_rgba(255,250,240,0.05)]">
          {turn.content}
        </p>
      </div>
    );
  }

  // No bubble on the assistant side. The answer is set as prose on the page
  // ground — a wall of text in a box reads like a receipt, not a reply.
  return (
    <div className="rise flex flex-col gap-4">
      {streaming && !turn.content && <Activity passages={turn.passages} />}

      {turn.content && (
        <div className="text-prose text-ink">
          <Prose text={turn.content} citations={turn.citations} />
          {streaming && <span className="caret" />}
        </div>
      )}

      {turn.note && <p className="text-2xs text-warn">{turn.note}</p>}

      {!streaming && turn.citations && turn.citations.length > 0 && (
        <SourceCards citations={turn.citations} />
      )}

      {!streaming && (turn.passages != null || turn.ms != null) && (
        <div className="flex items-center gap-2 text-2xs text-ink-ghost">
          {turn.passages != null && <span>{turn.passages} passages</span>}
          {turn.ms != null && (
            <>
              <span>·</span>
              <span className="num">{formatMs(turn.ms)}</span>
            </>
          )}
          {turn.cost != null && turn.cost > 0 && (
            <>
              <span>·</span>
              <span className="num">${turn.cost.toFixed(4)}</span>
            </>
          )}
          {turn.trace && (
            <>
              <span>·</span>
              <button
                type="button"
                onClick={() => onTrace(turn.trace ?? null)}
                className="underline decoration-line-strong underline-offset-4 transition-colors hover:text-ink-dim"
              >
                trace
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * What retrieval is doing, before there is anything to read.
 *
 * A spinner says "wait"; this says "I searched, and found six passages", which
 * is both a progress bar and the first half of the answer's evidence.
 */
function Activity({ passages }: { passages?: number }) {
  const text =
    passages == null
      ? "Searching Drive, Notion, Slack and GitHub…"
      : `Reading ${passages} passage${passages === 1 ? "" : "s"}…`;

  return (
    <div className="flex items-center gap-2.5">
      <span className="flex h-4 w-4 items-center justify-center">
        <span className="h-1.5 w-1.5 animate-[pulse-soft_1.4s_ease-in-out_infinite] rounded-full bg-ink-faint" />
      </span>
      <span className="shimmer text-sm">{text}</span>
    </div>
  );
}

/* ──────────────────────────────────────────────────────────────── prose ── */

/**
 * Renders the answer, turning [n] markers into source chips.
 *
 * A marker with no matching citation stays visible and turns red rather than
 * being quietly swallowed: a model that writes [7] when six passages were
 * retrieved should be obviously wrong, not tidied up.
 */
function Prose({ text, citations }: { text: string; citations?: Citation[] }) {
  if (!text) return null;
  const byMarker = new Map((citations ?? []).map((c) => [c.marker, c]));
  const blocks = text.split(/\n{2,}/);

  return (
    <>
      {blocks.map((block, b) => (
        <p key={b} className="mb-4 whitespace-pre-wrap last:mb-0">
          {block.split(/(\[\d{1,2}\])/g).map((part, i) => {
            const match = /^\[(\d{1,2})\]$/.exec(part);
            if (!match) return <span key={i}>{part}</span>;

            const marker = Number(match[1]);
            const citation = byMarker.get(marker);
            if (!citation) {
              return (
                <span key={i} className="text-danger" title="No such passage">
                  {part}
                </span>
              );
            }
            return <CiteChip key={i} citation={citation} />;
          })}
        </p>
      ))}
    </>
  );
}

function CiteChip({ citation }: { citation: Citation }) {
  const meta = sourceMeta(citation.source_kind);
  return (
    <span className="group relative inline-block align-baseline">
      <a href={`#cite-${citation.marker}`} className="cite">
        <meta.Glyph className="h-3 w-3 shrink-0 opacity-70" />
        <span className="truncate">{citation.document_title}</span>
      </a>
      {/* The popover answers "is this citation any good?" without a page jump. */}
      <span className="pointer-events-none absolute bottom-full left-0 z-30 mb-1.5 hidden w-72 flex-col gap-1 rounded-[9px] border border-line-strong bg-s3 p-2.5 shadow-[0_12px_28px_rgba(0,0,0,0.55)] group-hover:flex">
        <span className="flex items-center gap-1.5 text-2xs text-ink-faint">
          <meta.Glyph className="h-3 w-3" />
          {meta.label}
          {citation.page != null && <span className="num">· p.{citation.page}</span>}
        </span>
        <span className="line-clamp-4 text-xs leading-relaxed text-ink-dim">
          {citation.snippet}
        </span>
      </span>
    </span>
  );
}

/**
 * Sources as a horizontal row, not a bibliography.
 *
 * Reading order matters: the eye should sweep the sources once and stop, then
 * return to the answer. A vertical list invites reading all of them.
 */
function SourceCards({ citations }: { citations: Citation[] }) {
  return (
    <div className="flex gap-2 overflow-x-auto pb-1">
      {citations.map((c) => {
        const meta = sourceMeta(c.source_kind);
        const inner = (
          <>
            <span className="flex items-center gap-1.5 text-2xs text-ink-faint">
              <meta.Glyph className="h-3.5 w-3.5" />
              {meta.label}
              {c.page != null && <span className="num ml-auto">p.{c.page}</span>}
            </span>
            <span className="truncate text-xs text-ink">{c.document_title}</span>
            <span className="line-clamp-2 text-2xs leading-snug text-ink-ghost">
              {c.snippet}
            </span>
          </>
        );
        const cls =
          "surface surface-hover flex w-[13.5rem] shrink-0 scroll-mt-8 flex-col gap-1.5 p-2.5";

        return c.document_uri ? (
          <a
            key={c.marker}
            id={`cite-${c.marker}`}
            href={c.document_uri}
            target="_blank"
            rel="noreferrer"
            className={cls}
          >
            {inner}
          </a>
        ) : (
          <div key={c.marker} id={`cite-${c.marker}`} className={cls}>
            {inner}
          </div>
        );
      })}
    </div>
  );
}

/* ───────────────────────────────────────────────────────────── composer ── */

function Composer({
  value,
  onChange,
  onSubmit,
  onStop,
  busy,
}: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  busy: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  // Grow with the content up to a ceiling. Reset the height first, or the box
  // can only ever get taller.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
  }, [value]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20 px-6 pb-6">
      <div className="pointer-events-auto mx-auto w-full max-w-[45rem]">
        <div className="border-lit glass relative rounded-[14px] shadow-[0_18px_44px_rgba(0,0,0,0.55)]">
          <div className="flex items-end gap-2 p-2">
            <textarea
              ref={ref}
              rows={1}
              value={value}
              onChange={(e) => onChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  onSubmit();
                }
              }}
              placeholder="Ask across Drive, Notion, Slack…"
              className="max-h-[168px] flex-1 resize-none bg-transparent px-2 py-1.5 text-sm leading-relaxed text-ink outline-none placeholder:text-ink-ghost"
            />
            {busy ? (
              <button type="button" onClick={onStop} className="btn shrink-0">
                Stop
              </button>
            ) : (
              <button
                type="button"
                onClick={onSubmit}
                disabled={!value.trim()}
                className="btn btn-primary shrink-0"
              >
                Ask
              </button>
            )}
          </div>

          <ScopeBar />
        </div>
      </div>
    </div>
  );
}

/**
 * What the question will actually reach.
 *
 * These are indicators, not filters: `/chat` has no per-source scope parameter
 * yet, so a chip that looked clickable would be a lie. Sources with no
 * connector at all say Soon and link to where you would add one.
 */
function ScopeBar() {
  const [kinds, setKinds] = useState<Set<string> | null>(null);

  useEffect(() => {
    let alive = true;
    void api
      .sources()
      .then((r) => {
        if (alive) setKinds(new Set(r.sources.map((s) => s.kind)));
      })
      .catch(() => {
        if (alive) setKinds(new Set());
      });
    return () => {
      alive = false;
    };
  }, []);

  const shown: SourceKind[] = ["gdrive", "notion", "slack", "github", "upload"];

  return (
    <div className="flex items-center gap-1.5 border-t border-line px-3 py-1.5">
      <span className="text-2xs text-ink-ghost">Searching</span>
      {shown.map((kind) => {
        const meta = SOURCES[kind];
        const on = kinds?.has(kind) ?? false;
        return meta.live ? (
          <span
            key={kind}
            title={on ? `${meta.label} · connected` : `${meta.label} · not connected`}
            className={`flex items-center gap-1 rounded-[5px] px-1.5 py-0.5 text-2xs transition-colors ${
              on ? "bg-[rgba(255,250,240,0.05)] text-ink-dim" : "text-ink-ghost"
            }`}
          >
            <meta.Glyph className="h-3 w-3" />
            {meta.label}
          </span>
        ) : (
          <Link
            key={kind}
            href="/connectors"
            title={`${meta.label} — connector not built yet`}
            className="flex items-center gap-1 rounded-[5px] px-1.5 py-0.5 text-2xs text-ink-ghost transition-colors hover:text-ink-faint"
          >
            <meta.Glyph className="h-3 w-3 opacity-50" />
            {meta.label}
            <span className="opacity-60">Soon</span>
          </Link>
        );
      })}
      <span className="ml-auto hidden text-2xs text-ink-ghost sm:inline">
        <span className="num">⌘K</span> to focus
      </span>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────── drawer ── */

/**
 * The debugger, demoted.
 *
 * It used to own a third of the screen on every question. It is a power tool:
 * one link per answer, `T` from anywhere, and it slides over rather than
 * pushing the conversation around.
 */
function TraceDrawer({
  trace,
  onClose,
}: {
  trace: RetrievalTrace | null;
  onClose: () => void;
}) {
  if (!trace) return null;

  return (
    <div className="fixed inset-0 z-40">
      <button
        type="button"
        aria-label="Close trace"
        onClick={onClose}
        className="absolute inset-0 bg-[rgba(0,0,0,0.5)] backdrop-blur-[2px]"
      />
      <aside className="fade absolute inset-y-0 right-0 flex w-full max-w-[32rem] flex-col border-l border-line-strong bg-bg shadow-[-24px_0_60px_rgba(0,0,0,0.6)]">
        <header className="flex h-[52px] shrink-0 items-center justify-between border-b border-line px-4">
          <div className="flex items-baseline gap-2.5">
            <span className="text-sm font-medium">Retrieval trace</span>
            <span className="num text-2xs text-ink-ghost">
              {formatMs(trace.timings.total_ms)}
            </span>
          </div>
          <button type="button" onClick={onClose} className="btn h-7 px-2">
            Close
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <Inspector trace={trace} />
        </div>
      </aside>
    </div>
  );
}
