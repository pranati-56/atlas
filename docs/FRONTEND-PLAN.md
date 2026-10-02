# Atlas frontend — rebuild plan

Handoff brief. Written by the Fable session that shipped the current frontend,
for the Opus session that will replace it. The user rejected the current build;
their words: **"premium rich black colours"**, **"wth are these boxes"**,
**"the pattern is so ai generated"**, **"take inspiration from notions site
aswell i want something like that in black colours"**, and — most important —
**"the entire plan was to have a good rag app where we can connect our drive,
notion and slack and chat feels good."**

Read the whole file before writing code. The Anti-pattern rules and the
Verification bar at the bottom are binding, not advisory.

---

## 1 · What went wrong (do not repeat)

The current frontend (commits `9183e3b`, `498bf61`) fails four ways:

1. **Flat black, not rich black.** One page color, one panel color, uniform
   5.5% white hairlines on everything. No light model — nothing catches light,
   so the whole page reads as gray rectangles on a dead fill.
2. **Wireframe product shot.** The landing "product frame" is three equal
   columns of label+bar rows. It looks like a diagram of the product, not the
   product. Density is credibility; emptiness is a mockup.
3. **Template symmetry.** Three equal feature cards, four equal stat cells,
   centered closer — the layout rhythm of every generated page. Rich sites
   have a dominant object, asymmetric grids, and fewer, better sections.
4. **Wrong story.** The pitch led with the retrieval debugger ("Retrieval you
   can put on trial"). The product is: **connect your company's sources, ask
   questions, get answers with receipts that respect permissions.** The
   debugger demotes to a power feature.

What survives: the Instrument Sans / JetBrains Mono pairing, the NDJSON
streaming client in `src/lib/api.ts` (solid, keep), the corpus polling logic,
and the dense-indigo / lexical-amber channel colors — **scoped to the
Inspector only**, no longer the site-wide language.

North stars, one theft each:

- **Notion** (user-named): the real app UI is the hero object, huge and
  softly lit; warm editorial type with serif-italic accent words; friendly,
  human copy in short sentences. Translate all of it into black.
- **Linear**: restraint and jump-scale typography.
- **Raycast**: glass surfaces and a store of branded connector tiles.
- **Perplexity**: source-first chat UX (activity line, source cards).

Do not clone any of them.

---

## 2 · Product story

One sentence: *Atlas connects Drive, Notion, Slack and GitHub, and answers
questions with citations you can check — filtered by what each person is
allowed to see.*

Everything on the landing page sells that sentence. The color in this product
comes from **source brands** (Notion, Slack, Drive, GitHub glyphs), not from
decorative gradients. The proof moments, in order of persuasive power:

1. A real-looking chat answer whose citations carry **source icons**.
2. The same question answered differently for two users (permissions).
3. A sync ticker (edited in Notion 2m ago → already re-indexed).
4. The retrieval trace — last, for the technical reader.

Honesty rule: the backend today implements `upload`, `web`, `github`
connectors. The `sources` table already has enum kinds for `notion`, `slack`,
`gdrive`, `jira`. **In-app**, unimplemented connectors render with a `Soon`
badge — never "Connected". **On the landing page**, showing the full breadth
as the vision is normal marketing and fine.

---

## 3 · Visual system — "premium rich black", operationalized

Rich black = a light model, not a hue — and per the Notion reference, a
**warm** one. Rich blacks in print are warm blacks; the highlights below use
warm white, not clinical white. Replace the token set in `globals.css` with:

### Surfaces (warm-neutral ramp — drop the blue tint)

```
--bg:      #060605    page ground (warm near-black)
--bg-deep: #000000    behind hero glows only
--s1:      #0e0d0c    card resting
--s2:      #141312    card hover / raised
--s3:      #1b1a18    popover, drawer, command bar
--glass:   rgba(255,252,245,0.03) + backdrop-blur(24px)   nav, composer, rails
```

### Ink

```
--ink:       #f8f7f4   (warm white)
--ink-dim:   #b8b5ae
--ink-faint: #726f68
--ink-ghost: #45423d
```

### The light model (this is the "rich" part — apply mechanically)

- Every elevated surface gets BOTH:
  `box-shadow: inset 0 1px 0 rgba(255,250,240,.06)` **and**
  `background: linear-gradient(180deg, rgba(255,250,240,.025), transparent 42%), var(--s1)`.
  Tops are lighter than bottoms, always. The warm tint in the highlight is
  what makes the black read rich instead of clinical.
- **Borders are tiered, never uniform:** resting `rgba(255,250,240,.045)`,
  interactive `.09`, active/focused `.16`. Feature objects (hero window,
  composer) get a **gradient border** — mask-composited, `.15` at top fading
  to `.02` at bottom. Build one `.border-lit` utility; use it sparingly.
- **One glow per viewport, maximum.** Radial, huge (40rem+), 10–14% alpha,
  always anchored to an object (behind the hero window, under the closer CTA)
  — never free-floating in a corner.
- Product shots sit on a **floor**: a soft radial shadow beneath, and a
  vignette seating the window into the page.
- Keep the grain overlay from the current build (it kills gradient banding);
  drop the two corner washes.

### Accent & brand color

- One interactive accent: `--accent: #7c89ff`, hover one legible step up.
- Source brand colors appear ONLY as 10–14% radial glows behind glyph tiles
  on hover, and as citation-chip tint. Glyphs themselves are monochrome
  (`--ink` at 72%) from simple-icons paths, inlined as SVG. Brand glow hexes:
  Slack `#4A154B`, Drive `#4285F4`, GitHub neutral, Jira `#0052CC`,
  Linear `#5E6AD2`, Confluence `#2684FF`, Figma `#F24E1E`, Notion neutral.
- Channel colors (`#6572e8` dense / `#d99a4e` lexical) live only inside the
  Inspector and the one bento trace cell.

### Type

- Keep Instrument Sans (UI) + JetBrains Mono (numerals, tabular, always).
- Display: weight 620 (variable axis), tracking −0.045em, leading 0.92.
  Hero: `clamp(3.4rem, 8vw, 6.5rem)`. Section heads:
  `clamp(2rem, 4.4vw, 3.4rem)`. Nothing between 25px and 40px — jump scale.
- **Serif italic accent (Notion move, now recommended):** one accent word in
  the hero and in each section head set in **Newsreader Italic** (Google
  Fonts, load 500 italic only). E.g. "Ask your company *anything*." Use it
  exactly once per heading — twice is a costume.
- Chat prose: 16px / 1.75, column max 720px.
- Copy voice (Notion, not Linear): short human sentences, no jargon in
  headings, warmth allowed in microcopy ("Three connectors live today. The
  rest are on the bench.").

### Motion

- Landing: one staggered entrance on the hero (headline → sub → CTA → window,
  60ms steps). Gentle reveals (`opacity` + 6px rise) on section entry, once.
- App: 150ms ease-out on everything interactive; streaming shimmer on the
  activity line; citation chips lift a source-preview popover on hover.
- `prefers-reduced-motion` collapses all of it, as now.

---

## 4 · Pages

### 4.1 Landing `/`

Six sections. No more.

1. **Nav** — glass, 62px, hairline bottom. Wordmark · Product / Connectors
   (anchor links) · warm-white pill "Open app".
2. **Hero** — centered. Headline options (pick one, do not invent a fourth;
   italic word in Newsreader):
   - "Ask your company *anything*."
   - "Every answer your company *already has*."
   Sub (verbatim): "Atlas connects Drive, Notion, Slack and GitHub, and
   answers with citations you can check — filtered by what each person is
   allowed to see." CTA pair: pill "Open app" + ghost "How it works".
3. **The app window** — THE product shot, replacing the three boxes; the
   Notion move: the actual app, huge, softly lit. A real DOM render of the
   chat surface, styled identically to `/ask`: window chrome (traffic lights,
   sidebar with chat history), a full conversation, source cards, composer.
   **Mock content is specified in §6 — use it verbatim.** Gradient border,
   floor glow, vignette, slight bleed behind the next section. Minimum 25
   distinct content nodes.
4. **Connector belt** — one quiet row of 8 glass tiles (glyphs monochrome,
   brand glow on hover): Drive, Notion, Slack, GitHub, Jira, Linear,
   Confluence, Figma. Caption: "Three connectors live today. The rest are on
   the bench."
5. **Bento** — 12-col grid, asymmetric, four cells, heights vary:
   - **A (7 cols, tall):** "Answers with receipts" — two-turn mini chat with
     source-icon citation chips.
   - **B (5 cols):** "Respects permissions" — the same question answered for
     `alice@` (engineering) and `carol@` (public only), lock chip, visibly
     different source sets. This is the killer visual.
   - **C (5 cols):** "Always current" — sync ticker: "Notion · Auth decisions
     — edited 2m ago → re-indexed", three rows, one mid-animation.
   - **D (7 cols, short):** "See why" — ONE fused-result row from the
     debugger: `d1` + `l3` chips and a gradient bar. One row. Never three
     columns.
6. **Closer + footer** — "Your company already wrote *the answer*." + one
   CTA. Footer: wordmark, three links. Kill the stat band entirely.

### 4.2 App shell (all app routes)

Sidebar, Linear-app structure with Notion warmth, glass on `--bg`:

- Top: workspace block ("Acme · Atlas", the dual-arc mark).
- **"New chat" button + chat history list** (session titles, relative times;
  localStorage persistence is fine for now — design the list as if real).
- Nav: Ask · Connectors · Corpus · Debug (Debug pinned bottom with the
  channel legend, which moves here from the main rail).
- Health pill stays, restyled.

### 4.3 Chat `/ask` — "chat feels good" is the acceptance test

- **Assistant turns have no bubble.** Set prose on the page ground, 16/1.75.
  User turns: compact right-aligned pill on `--s1`.
- **Citations carry source icons.** `[1]` renders as a chip:
  `⬒ Notion · Auth decisions` (glyph + short title). Hover lifts a popover
  with the snippet. The bare-number chip dies. Invented markers still render
  red — keep that honesty.
- **Activity line before tokens** (replaces the always-open trace rail):
  "Searching GitHub, Notion, Drive…" → "Reading 6 passages" → streaming.
  Shimmer animation, then it collapses into the answer's meta row.
- **Source row after each answer** — horizontal cards (glyph, title, section,
  page), Perplexity-style, not a vertical list.
- **Trace demoted to a drawer**: a small "trace · 412ms" affordance per
  answer opens the full Inspector as a right slide-over (`T` toggles).
  Inspector component survives as-is inside the drawer, retinted.
- **Composer**: floating glass bar, gradient border, docked bottom-center,
  `⌘K` focuses, source-filter chips inside (All · Drive · Notion · Slack ·
  GitHub — unimplemented ones disabled with "Soon").
- **Empty state**: "Ask across {n} sources · {chunks} indexed passages" from
  `/documents` counts, connector status chips, three suggested questions.

### 4.4 Connectors `/connectors` — NEW, the page the user asked for

Raycast-store aesthetic on the warm ramp. Grid of connector cards on `--s1`:

- Card: monochrome glyph on a glass tile, name, status line
  ("Connected · synced 2m ago · 1,204 docs" from `GET /sources` /
  `document_count`), and either "Sync now" (`POST /sources/{id}/sync`) or a
  "Connect" button.
- `github`: Connect opens a modal → repo + token + branch →
  `POST /sources` then sync. `web`: URL-list modal. `upload`: routes to
  Corpus.
- `notion` / `slack` / `gdrive` / `jira`: rendered fully, `Soon` badge,
  Connect disabled. Never fake a connection.
- Brand glow on hover only.

### 4.5 Corpus & Debug

- Corpus: restyle to a Linear-density table (name · source glyph · chunks ·
  size · synced · status dot). Keep upload/polling logic. Kill card-per-row.
- Debug: keep functionally, retint to the new surface system. It is now the
  only place the three-column layout is allowed to exist — it is a tool
  there, not a pitch.

---

## 5 · Anti-pattern rules (binding)

1. **Never three equal columns** of icon + title + paragraph. Anywhere.
2. **Never N equal stat cells.** If a number matters, give it a sentence.
3. Any grid must be asymmetric: at least one cell 2× another in area.
4. Max one glow per viewport; zero free-floating gradients; no purple-on-black.
5. No dot grids, no particle fields, no orbiting-planets diagrams.
6. Every string in every mock is a plausible artifact name ("auth-service
   runbook", "Q3 incident review") — no lorem, no "Feature one", no
   "Document A".
7. Borders never uniform across a page — use the three tiers.
8. Serif italic accent: at most one word per heading, never in body copy.
9. If a section could be dropped into any SaaS landing page unchanged, cut it
   or make it Atlas-specific (source glyphs, permissions, citations).

## 6 · Mock content for the hero app window (use verbatim)

Sidebar history: "SSO session length", "Q3 churn drivers", "Deploy checklist
for eu-west", "Onboarding doc gaps". Active: first.

- **User:** What did we decide about SSO session length?
- **Atlas:** Sessions are capped at 12 hours, decided in the March incident
  review `[Notion · Auth decisions]`. The rollout finished May 14 — confirmed
  in `[Slack · #security]` — and the default lives in
  `[GitHub · auth/config.py]` as `SESSION_TTL_HOURS = 12`.
- Source row: three cards (Notion / Slack / GitHub glyphs, titles above,
  "updated 2m ago" on the Notion card).
- Composer placeholder: "Ask across Drive, Notion, Slack…"
- Meta row: "6 passages · 412ms · trace"

## 7 · Implementation order

1. `globals.css` — warm token ramp, light model, `.border-lit`, glass,
   retint primitives, load Newsreader Italic. Everything downstream inherits.
2. App shell — sidebar with chat history; move legend to Debug.
3. `/ask` rebuild — prose answers, icon citations, activity line, source row,
   trace drawer, glass composer.
4. `/connectors` — card grid wired to `GET /sources`, connect/sync modals for
   github/web, Soon badges for the rest.
5. Landing rebuild — hero, app-window mock (§6), belt, bento, closer.
6. Corpus table + Debug retint.
7. Verification (§8), then commit per numbered step, not one blob.

## 8 · Verification bar (do not self-report "looks good" without these)

- Computed-style probes via the browser tool: h1 ≥ 60px at 1280w; body bg
  `#060605`; a `.border-lit` element shows asymmetric border alpha; the hero
  contains exactly one Newsreader-italic node.
- Density: hero app window contains ≥ 25 distinct text/glyph nodes.
- Grep the rendered pages for "Feature", "Lorem", "Document A" → zero hits.
- Rule-check §5.1: no section renders 3 equal-width siblings with matching
  class lists (quick DOM probe).
- Screenshot if the Browser pane is displayed; otherwise say plainly that
  verification was DOM-level only.
- `tsc --noEmit` clean; every route 200s with the backend down (graceful
  degraded states, as now).
