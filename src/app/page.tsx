import Link from "next/link";
import {
  AtlasMark,
  BELT,
  GitHubGlyph,
  NotionGlyph,
  SOURCES,
  SlackGlyph,
} from "@/components/icons";

export default function Landing() {
  return (
    <div className="relative z-10 overflow-x-hidden">
      <Nav />
      <Hero />
      <Belt />
      <Bento />
      <Closer />
      <Footer />
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────── nav ── */

function Nav() {
  return (
    <header className="glass sticky top-0 z-40 border-b border-line">
      <div className="mx-auto flex h-[62px] w-full max-w-[70rem] items-center gap-8 px-6">
        <Link href="/" className="flex items-center gap-2.5">
          <AtlasMark className="h-[19px] w-[19px] text-ink" />
          <span className="text-[0.95rem] font-semibold tracking-[-0.025em]">
            Atlas
          </span>
        </Link>

        <nav className="hidden items-center gap-6 sm:flex">
          <a
            href="#sources"
            className="text-[0.8125rem] text-ink-dim transition-colors hover:text-ink"
          >
            Sources
          </a>
          <a
            href="#how"
            className="text-[0.8125rem] text-ink-dim transition-colors hover:text-ink"
          >
            How it works
          </a>
        </nav>

        <Link href="/ask" className="btn btn-primary ml-auto">
          Open app
        </Link>
      </div>
    </header>
  );
}

/* ───────────────────────────────────────────────────────────────── hero ── */

function Hero() {
  return (
    <section className="relative px-6 pb-4 pt-24 sm:pt-32">
      {/* The one glow on this viewport, anchored behind the window below —
          never floating in a corner. */}
      <div
        aria-hidden
        className="pointer-events-none absolute left-1/2 top-[27rem] h-[44rem] w-[70rem] -translate-x-1/2 -translate-y-1/2"
        style={{
          background:
            "radial-gradient(50% 50% at 50% 50%, rgba(255,247,232,0.11), transparent 70%)",
        }}
      />

      <div className="relative mx-auto w-full max-w-[70rem]">
        <div className="mx-auto max-w-[46rem] text-center">
          <h1 className="rise display text-[clamp(3.4rem,8vw,6.5rem)] text-ink">
            Ask your company <span className="accent">anything</span>.
          </h1>

          <p className="rise d2 mx-auto mt-7 max-w-[34rem] text-[1.0625rem] leading-[1.65] text-ink-dim">
            Atlas connects Drive, Notion, Slack and GitHub, and answers with
            citations you can check — filtered by what each person is allowed to
            see.
          </p>

          <div className="rise d3 mt-9 flex items-center justify-center gap-2.5">
            <Link href="/ask" className="btn btn-primary btn-lg">
              Open app
            </Link>
            <a href="#how" className="btn btn-lg">
              How it works
            </a>
          </div>
        </div>

        <AppWindow />
      </div>
    </section>
  );
}

/* ─────────────────────────────────────────────────────── the app window ── */

const HISTORY = [
  "SSO session length",
  "Q3 churn drivers",
  "Deploy checklist for eu-west",
  "Onboarding doc gaps",
];

const CARDS = [
  {
    Glyph: NotionGlyph,
    source: "Notion",
    title: "Auth decisions",
    detail: "March incident review",
    fresh: "updated 2m ago",
  },
  {
    Glyph: SlackGlyph,
    source: "Slack",
    title: "#security",
    detail: "Rollout thread · May 14",
    fresh: null,
  },
  {
    Glyph: GitHubGlyph,
    source: "GitHub",
    title: "auth/config.py",
    detail: "SESSION_TTL_HOURS = 12",
    fresh: null,
  },
];

/**
 * The product shot is the product.
 *
 * Not a diagram of it, not three boxes with bars in them — the real chat
 * surface, the same components and the same type as `/ask`, rendered at size
 * with one answer in it. Density is what makes a screenshot credible; an empty
 * frame reads as a mockup no matter how well it is lit.
 */
function AppWindow() {
  return (
    <div className="lift d4 floor relative mx-auto mt-20 w-full max-w-[64rem]">
      <div className="border-lit relative overflow-hidden rounded-[16px] bg-bg shadow-[0_40px_120px_rgba(0,0,0,0.75)]">
        <div className="flex h-9 items-center gap-2 border-b border-line bg-s1 px-3.5">
          <span className="h-[9px] w-[9px] rounded-full bg-[#3a3733]" />
          <span className="h-[9px] w-[9px] rounded-full bg-[#3a3733]" />
          <span className="h-[9px] w-[9px] rounded-full bg-[#3a3733]" />
          <span className="ml-3 text-2xs text-ink-ghost">Acme · Atlas</span>
        </div>

        <div className="flex min-h-[26rem]">
          <aside className="hidden w-[190px] shrink-0 flex-col border-r border-line bg-[rgba(255,250,240,0.012)] p-3 sm:flex">
            <div className="flex h-7 items-center gap-2 rounded-[6px] border border-line-strong bg-s2 px-2 text-2xs text-ink-dim">
              <span className="text-ink-faint">+</span> New chat
            </div>

            <span className="label mt-5 px-1.5 pb-1.5">Recent</span>
            <ul className="flex flex-col gap-px">
              {HISTORY.map((item, i) => (
                <li
                  key={item}
                  className={`truncate rounded-[5px] px-1.5 py-1.5 text-2xs ${
                    i === 0
                      ? "bg-[rgba(255,250,240,0.05)] text-ink"
                      : "text-ink-faint"
                  }`}
                >
                  {item}
                </li>
              ))}
            </ul>

            <div className="mt-auto flex items-center gap-1.5 border-t border-line pt-3">
              <span
                className="h-1.5 w-1.5 rounded-full bg-positive"
                style={{ boxShadow: "0 0 0 3px rgba(95,185,138,0.14)" }}
              />
              <span className="text-2xs text-ink-faint">4 sources synced</span>
            </div>
          </aside>

          <div className="flex min-w-0 flex-1 flex-col justify-between p-5 sm:p-7">
            <div className="flex flex-col gap-6">
              <div className="flex justify-end">
                <p className="max-w-[80%] rounded-[12px] rounded-br-[4px] border border-line-strong bg-s2 px-3.5 py-2.5 text-sm">
                  What did we decide about SSO session length?
                </p>
              </div>

              <div className="flex flex-col gap-4">
                <p className="text-prose text-ink">
                  Sessions are capped at 12 hours, decided in the March incident
                  review <Cite Glyph={NotionGlyph} label="Auth decisions" />. The
                  rollout finished May 14 — confirmed in{" "}
                  <Cite Glyph={SlackGlyph} label="#security" /> — and the default
                  lives in <Cite Glyph={GitHubGlyph} label="auth/config.py" /> as{" "}
                  <span className="num text-ink-dim">SESSION_TTL_HOURS = 12</span>.
                </p>

                <div className="flex gap-2 overflow-hidden">
                  {CARDS.map((card) => (
                    <article
                      key={card.title}
                      className="surface flex w-[13.5rem] shrink-0 flex-col gap-1.5 p-2.5"
                    >
                      <span className="flex items-center gap-1.5 text-2xs text-ink-faint">
                        <card.Glyph className="h-3.5 w-3.5" />
                        {card.source}
                        {card.fresh && (
                          <span className="ml-auto text-ink-ghost">
                            {card.fresh}
                          </span>
                        )}
                      </span>
                      <span className="truncate text-xs text-ink">
                        {card.title}
                      </span>
                      <span className="truncate text-2xs text-ink-ghost">
                        {card.detail}
                      </span>
                    </article>
                  ))}
                </div>

                <div className="flex items-center gap-2 text-2xs text-ink-ghost">
                  <span>6 passages</span>
                  <span>·</span>
                  <span className="num">412ms</span>
                  <span>·</span>
                  <span className="underline decoration-line-strong underline-offset-4">
                    trace
                  </span>
                </div>
              </div>
            </div>

            <div className="border-lit glass mt-8 rounded-[13px]">
              <div className="flex items-center gap-2 p-2.5">
                <span className="flex-1 px-1 text-sm text-ink-ghost">
                  Ask across Drive, Notion, Slack…
                </span>
                <span className="btn btn-primary h-7 px-2.5">Ask</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Seats the window into the page rather than pasting it onto it. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 bottom-0 h-32 rounded-b-[16px]"
        style={{
          background: "linear-gradient(180deg, transparent, var(--color-bg) 92%)",
        }}
      />
    </div>
  );
}

function Cite({
  Glyph,
  label,
}: {
  Glyph: (p: { className?: string }) => React.JSX.Element;
  label: string;
}) {
  return (
    <span className="cite">
      <Glyph className="h-3 w-3 shrink-0 opacity-70" />
      {label}
    </span>
  );
}

/* ───────────────────────────────────────────────────────────────── belt ── */

function Belt() {
  return (
    <section id="sources" className="px-6 pb-24 pt-16">
      <div className="mx-auto w-full max-w-[70rem]">
        <div className="flex flex-wrap items-center justify-center gap-2">
          {BELT.map((kind) => {
            const meta = SOURCES[kind];
            return (
              <span
                key={kind}
                title={meta.blurb}
                className="group relative flex h-[54px] w-[54px] items-center justify-center overflow-hidden rounded-[13px] border border-line bg-[rgba(255,252,245,0.02)] text-ink-dim transition-colors duration-200 hover:border-line-strong hover:text-ink"
              >
                <span
                  aria-hidden
                  className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-300 group-hover:opacity-100"
                  style={{
                    background: `radial-gradient(60% 60% at 50% 40%, ${meta.glow}, transparent 70%)`,
                  }}
                />
                <meta.Glyph className="relative h-5 w-5" />
              </span>
            );
          })}
        </div>

        <p className="mx-auto mt-6 max-w-[32rem] text-center text-xs leading-relaxed text-ink-faint">
          Slack, Notion, Drive, GitHub and Jira sync today. Each keeps its own
          cursor, so a re-sync reads what changed and nothing else.
        </p>
      </div>
    </section>
  );
}

/* ──────────────────────────────────────────────────────────────── bento ── */

function Bento() {
  return (
    <section id="how" className="px-6 pb-28">
      <div className="mx-auto w-full max-w-[70rem]">
        <h2 className="display max-w-[24rem] text-[clamp(2rem,4.4vw,3.4rem)] text-ink">
          Answers you can <span className="accent">check</span>.
        </h2>

        <div className="mt-12 grid gap-3 lg:grid-cols-12">
          <Receipts />
          <Permissions />
          <Freshness />
          <Trace />
        </div>
      </div>
    </section>
  );
}

function Receipts() {
  return (
    <article className="surface flex flex-col gap-5 p-6 lg:col-span-7 lg:row-span-2">
      <div>
        <h3 className="text-[1.0625rem] font-medium tracking-[-0.02em] text-ink">
          Every claim carries its passage
        </h3>
        <p className="mt-2 max-w-sm text-sm leading-relaxed text-ink-faint">
          Citations are not footnotes bolted on afterwards. The model is handed
          numbered passages and answers from them; a marker pointing nowhere
          renders in red rather than being quietly swallowed.
        </p>
      </div>

      <div className="flex flex-col gap-4 rounded-[11px] border border-line bg-[rgba(0,0,0,0.35)] p-4">
        <div className="flex justify-end">
          <p className="rounded-[10px] rounded-br-[3px] border border-line-strong bg-s2 px-3 py-2 text-xs">
            Why did checkout failures spike after v4.2?
          </p>
        </div>
        <p className="text-sm leading-[1.7] text-ink-dim">
          The retry budget was halved in{" "}
          <Cite Glyph={GitHubGlyph} label="payments/retry.py" />, which turned
          transient gateway timeouts into hard failures. Reverted on May 2 after{" "}
          <Cite Glyph={SlackGlyph} label="#incident-4412" />.
        </p>
        <div className="flex items-center gap-2 text-2xs text-ink-ghost">
          <span>4 passages</span>
          <span>·</span>
          <span className="num">318ms</span>
        </div>
      </div>
    </article>
  );
}

/**
 * The killer visual: one question, two people, two different answers — because
 * the permission predicate sits inside both channel scans rather than in a
 * filter applied afterwards.
 */
function Permissions() {
  return (
    <article className="surface flex flex-col gap-4 p-6 lg:col-span-5">
      <div>
        <h3 className="text-[1.0625rem] font-medium tracking-[-0.02em] text-ink">
          The same question, two answers
        </h3>
        <p className="mt-2 text-sm leading-relaxed text-ink-faint">
          Nobody retrieves what they cannot open.
        </p>
      </div>

      <p className="rounded-[8px] border border-line bg-[rgba(0,0,0,0.3)] px-3 py-2 text-xs text-ink-dim">
        &ldquo;What is the severity policy for a P1?&rdquo;
      </p>

      <div className="flex flex-col gap-2.5">
        <PermissionRow
          who="alice@acme.com"
          group="engineering"
          answer="Paged within 5 minutes, incident channel opened automatically."
          sources={["Notion · Oncall policy", "Slack · #incidents"]}
        />
        <PermissionRow
          who="carol@acme.com"
          group="public only"
          answer="I could not find that in anything you have access to."
          sources={[]}
        />
      </div>
    </article>
  );
}

function PermissionRow({
  who,
  group,
  answer,
  sources,
}: {
  who: string;
  group: string;
  answer: string;
  sources: string[];
}) {
  return (
    <div className="rounded-[9px] border border-line px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span className="num text-2xs text-ink-dim">{who}</span>
        <span className="chip chip-muted">{group}</span>
      </div>
      <p className="mt-1.5 text-xs leading-relaxed text-ink-faint">{answer}</p>
      {sources.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {sources.map((s) => (
            <span key={s} className="chip chip-muted">
              {s}
            </span>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-2xs text-ink-ghost">0 passages cleared the ACL</p>
      )}
    </div>
  );
}

const TICKER = [
  { source: "Notion", what: "Auth decisions", when: "2m ago", state: "indexed" },
  {
    source: "Slack",
    what: "#security · thread",
    when: "6m ago",
    state: "indexing",
  },
  { source: "GitHub", what: "auth/config.py", when: "1h ago", state: "unchanged" },
];

function Freshness() {
  return (
    <article className="surface flex flex-col gap-4 p-6 lg:col-span-5">
      <div>
        <h3 className="text-[1.0625rem] font-medium tracking-[-0.02em] text-ink">
          Current, without re-reading everything
        </h3>
        <p className="mt-2 text-sm leading-relaxed text-ink-faint">
          Content hashes and parser versions decide whether a document needs
          nothing, a re-embed, or a full re-parse.
        </p>
      </div>

      <ul className="flex flex-col gap-px overflow-hidden rounded-[9px] border border-line">
        {TICKER.map((row) => (
          <li
            key={row.what}
            className="flex items-center gap-2.5 bg-[rgba(0,0,0,0.25)] px-3 py-2.5"
          >
            <span className="text-2xs text-ink-faint">{row.source}</span>
            <span className="min-w-0 flex-1 truncate text-xs text-ink-dim">
              {row.what}
            </span>
            <span className="num text-2xs text-ink-ghost">{row.when}</span>
            <span
              className={
                row.state === "indexing"
                  ? "chip chip-dense animate-[pulse-soft_1.6s_ease-in-out_infinite]"
                  : row.state === "indexed"
                    ? "chip chip-live"
                    : "chip chip-muted"
              }
            >
              {row.state}
            </span>
          </li>
        ))}
      </ul>
    </article>
  );
}

/**
 * One row from the debugger, not the debugger. It is a power feature and it
 * earns exactly this much of the landing page: proof that the ranking is
 * inspectable, then out of the way.
 */
function Trace() {
  return (
    <article className="surface flex flex-col gap-4 p-6 lg:col-span-7">
      <div className="flex items-baseline justify-between gap-4">
        <h3 className="text-[1.0625rem] font-medium tracking-[-0.02em] text-ink">
          And when it is wrong, you can see why
        </h3>
        <span className="label shrink-0">Fused rank 1</span>
      </div>

      <div className="rounded-[9px] border border-line bg-[rgba(0,0,0,0.3)] px-3.5 py-3">
        <div className="flex items-baseline gap-2">
          <span className="min-w-0 flex-1 truncate text-xs text-ink">
            Auth decisions · Session lifetime
          </span>
          <span className="num text-2xs text-ink-dim">0.834</span>
        </div>
        <div className="bar mt-2">
          <span style={{ width: "83%" }} />
        </div>
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <span className="chip chip-dense">d1</span>
          <span className="chip chip-lexical">l3</span>
          <span className="chip chip-muted">both channels</span>
        </div>
      </div>

      <p className="text-sm leading-relaxed text-ink-faint">
        A vector search finds what you meant; a keyword search finds what you
        typed. Weighted reciprocal-rank fusion decides between them, and the
        trace shows which channel earned each passage.
      </p>
    </article>
  );
}

/* ─────────────────────────────────────────────────────────────── closer ── */

function Closer() {
  return (
    <section className="relative px-6 pb-32 pt-8">
      <div
        aria-hidden
        className="pointer-events-none absolute left-1/2 top-1/2 h-[30rem] w-[52rem] -translate-x-1/2 -translate-y-1/2"
        style={{
          background:
            "radial-gradient(50% 50% at 50% 50%, rgba(255,247,232,0.1), transparent 70%)",
        }}
      />
      <div className="relative mx-auto max-w-[36rem] text-center">
        <h2 className="display text-[clamp(2.2rem,5vw,3.8rem)] text-ink">
          Your company already wrote <span className="accent">the answer</span>.
        </h2>
        <p className="mx-auto mt-5 max-w-[26rem] text-sm leading-relaxed text-ink-dim">
          It is in a Notion page nobody remembers, a Slack thread from March, and
          one line of a config file. Atlas finds all three.
        </p>
        <Link href="/ask" className="btn btn-primary btn-lg mt-8">
          Open app
        </Link>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-line px-6 py-8">
      <div className="mx-auto flex w-full max-w-[70rem] flex-wrap items-center gap-x-6 gap-y-3">
        <span className="flex items-center gap-2 text-2xs text-ink-faint">
          <AtlasMark className="h-3.5 w-3.5" />
          Atlas
        </span>
        <Link
          href="/connectors"
          className="text-2xs text-ink-ghost transition-colors hover:text-ink-dim"
        >
          Connectors
        </Link>
        <Link
          href="/corpus"
          className="text-2xs text-ink-ghost transition-colors hover:text-ink-dim"
        >
          Corpus
        </Link>
        <Link
          href="/debug"
          className="text-2xs text-ink-ghost transition-colors hover:text-ink-dim"
        >
          Retrieval debugger
        </Link>
      </div>
    </footer>
  );
}
