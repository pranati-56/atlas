"use client";

import { useState } from "react";
import {
  channelBias,
  formatMs,
  type RetrievalTrace,
  type RetrievedChunk,
} from "@/lib/api";

/**
 * The retrieval debugger.
 *
 * Three columns showing the same shortlist from three angles: what the vector
 * channel ranked, what the lexical channel ranked, and what fusion made of
 * both. The point is disagreement — a passage sitting at dense rank 11 and
 * lexical rank 2 is exactly the row that justifies running two retrievers, and
 * it is invisible in any interface that only shows the final order.
 */
export function Inspector({ trace }: { trace: RetrievalTrace | null }) {
  if (!trace) return <InspectorEmpty />;

  const { dense, sparse, fused } = trace;

  return (
    <div className="flex flex-col gap-5">
      <StageChart trace={trace} />

      {trace.rewritten_query && (
        <div className="surface fade p-3">
          <span className="label">Rewritten query</span>
          <p className="mt-1.5 text-sm text-ink-dim">
            <span className="text-ink-ghost line-through">{trace.query}</span>
            <span className="mx-2 text-ink-ghost">→</span>
            <span className="text-ink">{trace.rewritten_query}</span>
          </p>
        </div>
      )}

      {fused.length > 0 ? (
        <div className="grid gap-3 lg:grid-cols-3">
          <Column
            title="Dense"
            note="cosine over HNSW"
            tone="dense"
            chunks={dense}
            score={(c) => c.dense_score}
            rank={(c) => c.dense_rank}
          />
          <Column
            title="Lexical"
            note="BM25 over the tsvector"
            tone="lexical"
            chunks={sparse}
            score={(c) => c.sparse_score}
            rank={(c) => c.sparse_rank}
          />
          <Column
            title="Fused"
            note={`weighted RRF · α ${trace.params.alpha.toFixed(2)}`}
            tone="fused"
            chunks={fused}
            score={(c) => c.fused_score}
            rank={(_c, i) => i + 1}
            showBias
          />
        </div>
      ) : (
        <div className="surface px-4 py-8 text-center">
          <p className="text-sm text-ink-dim">Nothing cleared the threshold.</p>
          <p className="mx-auto mt-1.5 max-w-md text-xs leading-relaxed text-ink-ghost">
            Either the corpus does not cover this question, or retrieval is too
            tight. Lower <span className="num text-ink-faint">min score</span>,
            raise <span className="num text-ink-faint">candidates</span>, or push{" "}
            <span className="num text-ink-faint">α</span> toward the lexical
            channel if the question contains identifiers or exact strings.
          </p>
        </div>
      )}

      <Totals trace={trace} />
    </div>
  );
}

function InspectorEmpty() {
  return (
    <div className="surface flex min-h-[220px] flex-col items-center justify-center gap-2 px-6 text-center">
      <div
        className="h-[3px] w-24 rounded-full opacity-40"
        style={{
          background:
            "linear-gradient(90deg, var(--color-dense), var(--color-lexical))",
        }}
      />
      <p className="mt-1 text-sm text-ink-dim">No trace yet</p>
      <p className="max-w-sm text-xs leading-relaxed text-ink-ghost">
        Ask something and this fills with the two channels, what each ranked,
        and how fusion resolved the disagreement.
      </p>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────── stage chart ── */

const STAGE_TONE: Record<string, string> = {
  embed: "var(--color-ink-faint)",
  dense: "var(--color-dense)",
  lexical: "var(--color-lexical)",
  fuse: "var(--color-accent)",
  rerank: "#8b6fd4",
  generate: "var(--color-positive)",
};

/**
 * A real flame chart, not an estimate: each channel is timed independently
 * inside the SQL function with clock_timestamp(), which is why the two run as
 * separate statements rather than as CTEs of one query.
 */
function StageChart({ trace }: { trace: RetrievalTrace }) {
  const t = trace.timings;
  const stages = [
    { key: "embed", label: "Embed query", ms: t.embed_ms },
    { key: "dense", label: "Dense", ms: t.dense_ms },
    { key: "lexical", label: "Lexical", ms: t.sparse_ms },
    { key: "fuse", label: "Fuse", ms: t.fuse_ms },
    ...(t.rerank_ms != null
      ? [{ key: "rerank", label: "Rerank", ms: t.rerank_ms }]
      : []),
    ...(t.generate_ms != null
      ? [{ key: "generate", label: "Generate", ms: t.generate_ms }]
      : []),
  ].filter((s) => s.ms > 0);

  const total = Math.max(
    stages.reduce((sum, s) => sum + s.ms, 0),
    1,
  );

  return (
    <div className="surface p-3.5">
      <div className="flex items-baseline justify-between">
        <span className="label">Stages</span>
        <span className="num text-xs text-ink-dim">{formatMs(total)}</span>
      </div>

      {/* Single stacked track: proportions are the message, and stacking makes
          the dominant stage obvious without reading a single number. */}
      <div className="mt-2.5 flex h-1.5 gap-px overflow-hidden rounded-full bg-[rgba(255,250,240,0.05)]">
        {stages.map((s, i) => (
          <div
            key={s.key}
            className="rise h-full first:rounded-l-full last:rounded-r-full"
            style={{
              width: `${(s.ms / total) * 100}%`,
              background: STAGE_TONE[s.key],
              opacity: 0.85,
              animationDelay: `${i * 45}ms`,
            }}
            title={`${s.label} · ${formatMs(s.ms)}`}
          />
        ))}
      </div>

      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
        {stages.map((s) => (
          <div key={s.key} className="flex items-center gap-1.5">
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-[2px]"
              style={{ background: STAGE_TONE[s.key] }}
            />
            <span className="truncate text-2xs text-ink-faint">{s.label}</span>
            <span className="num ml-auto text-2xs text-ink-dim">
              {formatMs(s.ms)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────── columns ── */

interface ColumnProps {
  title: string;
  note: string;
  tone: "dense" | "lexical" | "fused";
  chunks: RetrievedChunk[];
  score: (c: RetrievedChunk) => number | null;
  rank: (c: RetrievedChunk, i: number) => number | null;
  showBias?: boolean;
}

function Column({ title, note, tone, chunks, score, rank, showBias }: ColumnProps) {
  const accent =
    tone === "dense"
      ? "var(--color-dense)"
      : tone === "lexical"
        ? "var(--color-lexical)"
        : "linear-gradient(90deg, var(--color-dense), var(--color-lexical))";

  return (
    <section className="surface flex min-w-0 flex-col">
      <header className="flex items-baseline justify-between border-b border-line px-3 py-2.5">
        <div className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: accent }} />
          <h3 className="text-xs font-medium text-ink">{title}</h3>
        </div>
        <span className="num text-2xs text-ink-ghost">{chunks.length}</span>
      </header>

      <p className="px-3 pt-2 text-2xs text-ink-ghost">{note}</p>

      <div className="flex flex-col gap-1 p-2">
        {chunks.length === 0 && (
          <p className="px-1 py-4 text-center text-2xs text-ink-ghost">
            No hits on this channel
          </p>
        )}
        {chunks.map((c, i) => (
          <ChunkRow
            key={`${c.chunk_id}-${tone}`}
            chunk={c}
            index={i}
            rank={rank(c, i)}
            score={score(c)}
            tone={tone}
            showBias={showBias}
          />
        ))}
      </div>
    </section>
  );
}

function ChunkRow({
  chunk,
  index,
  rank,
  score,
  tone,
  showBias,
}: {
  chunk: RetrievedChunk;
  index: number;
  rank: number | null;
  score: number | null;
  tone: "dense" | "lexical" | "fused";
  showBias?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const bias = channelBias(chunk);

  // Fusion bars mix at the row's actual channel balance, so a bar leaning
  // indigo was won on semantics and one leaning amber on keywords.
  const fill =
    tone === "fused"
      ? `linear-gradient(90deg, var(--color-dense) 0%, var(--color-lexical) ${Math.round(
          (1 - bias) * 70 + 30,
        )}%)`
      : tone === "dense"
        ? "var(--color-dense)"
        : "var(--color-lexical)";

  const width =
    tone === "lexical" && score != null
      ? // BM25 is unbounded; squash it so one dominant hit does not flatten
        // every other bar in the column to nothing.
        `${Math.min(100, (score / (score + 4)) * 100 + 12)}%`
      : `${Math.max(4, Math.min(100, (score ?? 0) * 100))}%`;

  return (
    <article
      className="rise rounded-[7px] border border-transparent px-2 py-2 transition-colors duration-150 hover:border-line hover:bg-[rgba(255,250,240,0.025)]"
      style={{ animationDelay: `${Math.min(index * 30, 240)}ms` }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-2 text-left"
      >
        <span className="num mt-px w-4 shrink-0 text-2xs text-ink-ghost">
          {rank ?? "—"}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs text-ink">
            {chunk.heading ?? chunk.document_title}
          </span>
          <span className="mt-0.5 block truncate text-2xs text-ink-ghost">
            {chunk.source_kind} · {chunk.external_id}
            {chunk.page != null && ` · p.${chunk.page}`}
          </span>
        </span>
        <span className="num shrink-0 text-2xs text-ink-dim">
          {score == null ? "—" : score < 1 ? score.toFixed(3) : score.toFixed(2)}
        </span>
      </button>

      <div className="bar mt-1.5">
        <span style={{ width, background: fill }} />
      </div>

      {showBias && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          {chunk.dense_rank != null && (
            <span className="chip chip-dense">d{chunk.dense_rank}</span>
          )}
          {chunk.sparse_rank != null && (
            <span className="chip chip-lexical">l{chunk.sparse_rank}</span>
          )}
          {chunk.rerank_score != null && (
            <span className="chip chip-muted">
              rr {chunk.rerank_score.toFixed(2)}
            </span>
          )}
          {/* The row that justifies the architecture: found by one channel,
              missed entirely by the other. */}
          {(chunk.dense_rank == null || chunk.sparse_rank == null) && (
            <span className="chip chip-muted">single channel</span>
          )}
        </div>
      )}

      {open && (
        <pre className="fade mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-[6px] border border-line bg-deep px-2.5 py-2 font-mono text-2xs leading-relaxed text-ink-dim">
          {chunk.content}
        </pre>
      )}
    </article>
  );
}

/* ─────────────────────────────────────────────────────────────── totals ── */

function Totals({ trace }: { trace: RetrievalTrace }) {
  const overlap = trace.fused.filter(
    (c) => c.dense_rank != null && c.sparse_rank != null,
  ).length;

  const cells = [
    { label: "Context", value: `${trace.context_tokens} tok` },
    { label: "Returned", value: String(trace.fused.length) },
    { label: "Both channels", value: `${overlap}/${trace.fused.length}` },
    { label: "Candidates", value: String(trace.params.candidates) },
  ];

  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-[13px] border border-line bg-line sm:grid-cols-4">
      {cells.map((c) => (
        <div key={c.label} className="bg-s1 px-3 py-2.5">
          <span className="label">{c.label}</span>
          <p className="num mt-0.5 text-sm text-ink">{c.value}</p>
        </div>
      ))}
    </div>
  );
}
