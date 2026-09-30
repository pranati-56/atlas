"use client";

import { useCallback, useState } from "react";
import { Inspector } from "@/components/Inspector";
import {
  ApiError,
  DEFAULT_PARAMS,
  api,
  type RetrievalParams,
  type RetrievalTrace,
} from "@/lib/api";

/**
 * Retrieval without generation.
 *
 * Every knob is live and the trace re-renders on each run, so the effect of a
 * parameter is something you watch rather than something you infer. No
 * completion is requested here, which is what makes sweeping α across a dozen
 * runs cost nothing but latency.
 */
export default function DebugPage() {
  const [query, setQuery] = useState("");
  const [params, setParams] = useState<RetrievalParams>(DEFAULT_PARAMS);
  const [trace, setTrace] = useState<RetrievalTrace | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (next: RetrievalParams, q: string) => {
      const text = q.trim();
      if (!text) return;
      setBusy(true);
      setError(null);
      try {
        const res = await api.search(text, next);
        setTrace(res.trace);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Search failed.");
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  // Re-run on release rather than on every pixel of a drag: each run is a real
  // embedding call plus two index scans.
  const commit = (patch: Partial<RetrievalParams>) => {
    const next = { ...params, ...patch };
    setParams(next);
    if (trace) void run(next, query);
  };

  return (
    <div className="flex h-screen flex-col">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-line px-6">
        <div>
          <h1 className="text-[0.9rem] font-medium tracking-[-0.015em]">Debug</h1>
          <p className="text-2xs text-ink-ghost">
            Retrieval only — no completion, no cost
          </p>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[72rem] px-6 py-6">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void run(params, query);
            }}
            className="flex gap-2"
          >
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Query the index directly…"
              className="field flex-1"
            />
            <button
              type="submit"
              disabled={busy || !query.trim()}
              className="btn btn-primary shrink-0"
            >
              {busy ? "Running…" : "Run"}
            </button>
          </form>

          <Controls params={params} onCommit={commit} disabled={busy} />

          {error && (
            <div className="fade mt-4 rounded-[9px] border border-[rgba(224,115,109,0.28)] bg-[rgba(224,115,109,0.07)] px-3.5 py-2.5">
              <p className="text-xs text-[#eda6a1]">{error}</p>
            </div>
          )}

          <div className="mt-5">
            <Inspector trace={trace} />
          </div>
        </div>
      </div>
    </div>
  );
}

function Controls({
  params,
  onCommit,
  disabled,
}: {
  params: RetrievalParams;
  onCommit: (patch: Partial<RetrievalParams>) => void;
  disabled: boolean;
}) {
  return (
    <div className="surface mt-4 p-4">
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        <Slider
          label="α"
          hint={
            params.alpha > 0.65
              ? "Favouring meaning"
              : params.alpha < 0.35
                ? "Favouring keywords"
                : "Balanced"
          }
          value={params.alpha}
          min={0}
          max={1}
          step={0.05}
          format={(v) => v.toFixed(2)}
          gradient
          onCommit={(v) => onCommit({ alpha: v })}
          disabled={disabled}
        />
        <Slider
          label="top k"
          hint="Passages reaching the model"
          value={params.top_k}
          min={1}
          max={30}
          step={1}
          format={String}
          onCommit={(v) => onCommit({ top_k: v })}
          disabled={disabled}
        />
        <Slider
          label="candidates"
          hint="Pulled from each channel"
          value={params.candidates}
          min={10}
          max={200}
          step={10}
          format={String}
          onCommit={(v) => onCommit({ candidates: v })}
          disabled={disabled}
        />
        <Slider
          label="min score"
          hint="Fused cutoff"
          value={params.min_score}
          min={0}
          max={1}
          step={0.05}
          format={(v) => v.toFixed(2)}
          onCommit={(v) => onCommit({ min_score: v })}
          disabled={disabled}
        />
      </div>

      <div className="mt-4 flex flex-wrap gap-2 border-t border-line pt-3.5">
        <Toggle
          label="Rerank"
          hint="Second pass, scores query and passage jointly"
          on={params.rerank}
          onChange={(v) => onCommit({ rerank: v })}
          disabled={disabled}
        />
        <Toggle
          label="Rewrite"
          hint="Expand follow-ups into standalone queries"
          on={params.rewrite_query}
          onChange={(v) => onCommit({ rewrite_query: v })}
          disabled={disabled}
        />
      </div>
    </div>
  );
}

function Slider({
  label,
  hint,
  value,
  min,
  max,
  step,
  format,
  gradient,
  onCommit,
  disabled,
}: {
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format: (v: number) => string;
  gradient?: boolean;
  onCommit: (v: number) => void;
  disabled: boolean;
}) {
  // Local while dragging, committed on release — the label tracks the thumb
  // without firing a query per pixel.
  const [draft, setDraft] = useState(value);
  const shown = disabled ? value : draft;
  const pct = ((shown - min) / (max - min)) * 100;

  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex items-baseline justify-between">
        <span className="label">{label}</span>
        <span className="num text-xs text-ink">{format(shown)}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={shown}
        disabled={disabled}
        onChange={(e) => setDraft(Number(e.target.value))}
        onPointerUp={(e) => onCommit(Number(e.currentTarget.value))}
        onKeyUp={(e) => onCommit(Number(e.currentTarget.value))}
        className="h-1 w-full cursor-pointer appearance-none rounded-full outline-none [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border [&::-webkit-slider-thumb]:border-line-lit [&::-webkit-slider-thumb]:bg-ink"
        style={{
          // α gets the channel gradient itself: the control looks like the thing
          // it controls, so which way to drag needs no explaining.
          background: gradient
            ? "linear-gradient(90deg, var(--color-lexical), var(--color-dense))"
            : `linear-gradient(90deg, var(--color-accent) ${pct}%, rgba(255,255,255,0.06) ${pct}%)`,
        }}
      />
      <span className="text-2xs text-ink-ghost">{hint}</span>
    </label>
  );
}

function Toggle({
  label,
  hint,
  on,
  onChange,
  disabled,
}: {
  label: string;
  hint: string;
  on: boolean;
  onChange: (v: boolean) => void;
  disabled: boolean;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!on)}
      disabled={disabled}
      title={hint}
      className={`btn ${on ? "border-accent bg-[rgba(124,137,255,0.14)] text-ink" : ""}`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${on ? "bg-accent" : "bg-ink-ghost"}`}
      />
      {label}
    </button>
  );
}
