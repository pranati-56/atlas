"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { sourceMeta } from "@/components/icons";
import {
  ApiError,
  api,
  formatBytes,
  type CorpusCounts,
  type DocumentRow,
  type Stage,
} from "@/lib/api";

/** Ingest stages, in pipeline order. Drives the progress rail on a live row. */
const STAGES: Stage[] = [
  "queued",
  "extracting",
  "chunking",
  "embedding",
  "indexing",
  "ready",
];

const LIVE: Stage[] = [
  "queued",
  "fetching",
  "extracting",
  "chunking",
  "embedding",
  "indexing",
];

export default function CorpusPage() {
  const [docs, setDocs] = useState<DocumentRow[]>([]);
  const [counts, setCounts] = useState<CorpusCounts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.documents();
      setDocs(res.documents);
      setCounts(res.counts);
      setError(null);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load the corpus.",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll only while something is actually moving through the pipeline. A table
  // of finished documents does not need a heartbeat.
  useEffect(() => {
    const busy = docs.some((d) => LIVE.includes(d.stage));
    if (!busy && uploading === 0) return;
    const id = setInterval(() => void load(), 1200);
    return () => clearInterval(id);
  }, [docs, uploading, load]);

  const upload = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files);
      if (list.length === 0) return;
      setUploading((n) => n + list.length);
      setError(null);
      for (const file of list) {
        try {
          await api.upload(file);
        } catch (err) {
          setError(
            err instanceof ApiError
              ? `${file.name}: ${err.message}`
              : `${file.name}: upload failed.`,
          );
        } finally {
          setUploading((n) => n - 1);
        }
      }
      void load();
    },
    [load],
  );

  return (
    <div
      className="min-h-screen"
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        void upload(e.dataTransfer.files);
      }}
    >
      <div className="mx-auto w-full max-w-[62rem] px-8 pb-24 pt-14">
        <header className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="display text-[2.4rem] text-ink">
              What Atlas can <span className="accent">read</span>.
            </h1>
            {counts && (
              <p className="mt-3 text-sm text-ink-dim">
                <span className="num text-ink">
                  {counts.documents.toLocaleString()}
                </span>{" "}
                documents, cut into{" "}
                <span className="num text-ink">
                  {counts.chunks.toLocaleString()}
                </span>{" "}
                passages.{" "}
                {counts.failed > 0 ? (
                  <span className="text-warn">
                    {counts.failed} failed to index.
                  </span>
                ) : (
                  <span className="text-ink-faint">Everything indexed.</span>
                )}
              </p>
            )}
          </div>

          <button
            type="button"
            onClick={() => input.current?.click()}
            className="btn btn-primary"
          >
            Add documents
          </button>
          <input
            ref={input}
            type="file"
            multiple
            hidden
            accept=".pdf,.docx,.md,.markdown,.txt,.html,.htm,.py,.ts,.tsx,.js,.jsx,.go,.rs,.java,.rb,.sql,.yaml,.yml,.json"
            onChange={(e) => {
              if (e.target.files) void upload(e.target.files);
              e.target.value = "";
            }}
          />
        </header>

        {error && (
          <div className="fade mt-6 rounded-[9px] border border-[rgba(224,115,109,0.28)] bg-[rgba(224,115,109,0.07)] px-3.5 py-2.5">
            <p className="text-xs leading-relaxed text-[#eda6a1]">{error}</p>
          </div>
        )}

        {uploading > 0 && (
          <p className="mt-6 text-xs text-ink-faint">
            Uploading {uploading} file{uploading > 1 ? "s" : ""}…
          </p>
        )}

        <section className="mt-10">
          {/* A table, not a card per row. At a hundred documents the card
              layout is a scroll marathon; a dense table is scannable. */}
          <div className="grid grid-cols-[1fr_auto_auto_auto_auto] items-center gap-x-4 border-b border-line pb-2">
            <span className="label">Document</span>
            <span className="label hidden sm:block">Passages</span>
            <span className="label hidden sm:block">Size</span>
            <span className="label">Stage</span>
            <span />
          </div>

          {docs.length === 0 ? (
            <Empty dragging={dragging} />
          ) : (
            <div className="flex flex-col">
              {docs.map((doc) => (
                <Row
                  key={doc.id}
                  doc={doc}
                  onDelete={async () => {
                    // Optimistic: the row is gone from the server the moment the
                    // request lands, and waiting a round trip to admit it makes
                    // the table feel broken.
                    setDocs((prev) => prev.filter((d) => d.id !== doc.id));
                    try {
                      await api.deleteDocument(doc.id);
                    } finally {
                      void load();
                    }
                  }}
                />
              ))}
            </div>
          )}
        </section>
      </div>

      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-[rgba(0,0,0,0.5)]">
          <div className="border-lit rounded-[14px] bg-s3 px-6 py-4 text-sm text-ink">
            Drop to index
          </div>
        </div>
      )}
    </div>
  );
}

function Empty({ dragging }: { dragging: boolean }) {
  return (
    <div
      className={`mt-4 rounded-[13px] border border-dashed px-6 py-14 text-center transition-colors duration-150 ${
        dragging ? "border-line-lit" : "border-line-strong"
      }`}
    >
      <p className="text-sm text-ink-dim">Nothing indexed yet</p>
      <p className="mx-auto mt-1.5 max-w-sm text-2xs leading-relaxed text-ink-ghost">
        Drop files here, or connect Slack, Notion, Drive, GitHub or Jira and let
        them fill this in.
      </p>
    </div>
  );
}

function Row({ doc, onDelete }: { doc: DocumentRow; onDelete: () => void }) {
  const live = LIVE.includes(doc.stage);
  const stageIndex = STAGES.indexOf(doc.stage);
  const meta = sourceMeta(doc.source_kind);

  return (
    <article className="group grid grid-cols-[1fr_auto_auto_auto_auto] items-center gap-x-4 border-b border-line py-2.5 transition-colors duration-150 hover:bg-[rgba(255,250,240,0.018)]">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="shrink-0 text-ink-ghost" title={doc.source_name}>
          <meta.Glyph className="h-3.5 w-3.5" />
        </span>
        <div className="min-w-0">
          <p className="truncate text-sm text-ink">{doc.title}</p>
          {doc.error ? (
            <p className="mt-0.5 truncate text-2xs text-[#eda6a1]">{doc.error}</p>
          ) : (
            <p className="mt-0.5 truncate text-2xs text-ink-ghost">
              {doc.source_name} · {doc.kind}
            </p>
          )}
          {live && (
            <div className="mt-1.5 flex max-w-[16rem] gap-px overflow-hidden rounded-full">
              {STAGES.slice(0, -1).map((s, i) => (
                <div
                  key={s}
                  className="h-[3px] flex-1 transition-colors duration-300"
                  style={{
                    background:
                      i < stageIndex
                        ? "var(--color-ink-faint)"
                        : i === stageIndex
                          ? "var(--color-accent)"
                          : "rgba(255,250,240,0.06)",
                  }}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      <span className="num hidden text-2xs text-ink-faint sm:block">
        {doc.chunk_count > 0 ? doc.chunk_count.toLocaleString() : "—"}
      </span>
      <span className="num hidden text-2xs text-ink-ghost sm:block">
        {formatBytes(doc.size_bytes)}
      </span>

      <StageBadge doc={doc} />

      <button
        type="button"
        onClick={onDelete}
        title="Delete"
        className="rounded-[5px] px-1.5 py-1 text-2xs text-ink-ghost opacity-0 transition-all duration-150 hover:bg-[rgba(224,115,109,0.1)] hover:text-danger focus-visible:opacity-100 group-hover:opacity-100"
      >
        Delete
      </button>
    </article>
  );
}

function StageBadge({ doc }: { doc: DocumentRow }) {
  if (doc.stage === "ready") {
    return (
      <span className="flex items-center gap-1.5 text-2xs text-ink-faint">
        <span className="h-1.5 w-1.5 rounded-full bg-positive" />
        ready
      </span>
    );
  }
  if (doc.stage === "failed") {
    return (
      <span className="flex items-center gap-1.5 text-2xs text-[#eda6a1]">
        <span className="h-1.5 w-1.5 rounded-full bg-danger" />
        failed
      </span>
    );
  }
  if (doc.stage === "skipped") {
    return <span className="text-2xs text-ink-ghost">unchanged</span>;
  }
  return (
    <span className="flex items-center gap-1.5 text-2xs text-ink-dim">
      <span className="h-1.5 w-1.5 animate-[pulse-soft_1.4s_ease-in-out_infinite] rounded-full bg-accent" />
      {doc.stage}
      {doc.stage === "embedding" && doc.progress > 0
        ? ` ${Math.round(doc.progress * 100)}%`
        : ""}
    </span>
  );
}
