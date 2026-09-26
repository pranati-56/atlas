/**
 * Source glyphs.
 *
 * These are the only "brand" in the product. They render monochrome — the tool
 * has one voice, and eight logo palettes fighting on a black page is a toolbar,
 * not a design. Brand colour appears once, as a soft radial behind the tile on
 * hover, which is enough to say "that's Slack" without repainting the UI.
 *
 * Everything is drawn on a 24×24 box with `currentColor` fill so the glyphs
 * inherit the ink ramp like text does.
 */

export type SourceKind =
  | "notion"
  | "slack"
  | "gdrive"
  | "github"
  | "jira"
  | "linear"
  | "confluence"
  | "figma"
  | "web"
  | "upload";

type GlyphProps = { className?: string };

function Svg({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden
      className={className ?? "h-[18px] w-[18px]"}
    >
      {children}
    </svg>
  );
}

export function GitHubGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" />
    </Svg>
  );
}

export function SlackGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z" />
    </Svg>
  );
}

export function NotionGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path d="M4.459 4.208c.746.606 1.026.56 2.428.466l13.215-.793c.28 0 .047-.28-.046-.326L17.86 1.968c-.42-.326-.981-.7-2.055-.607L3.01 2.295c-.466.046-.56.28-.374.466zm.793 3.08v13.904c0 .747.373 1.027 1.214.98l14.523-.84c.841-.046.935-.56.935-1.167V6.354c0-.606-.233-.933-.748-.887l-15.177.887c-.56.047-.747.327-.747.933zm14.337.745c.093.42 0 .84-.42.888l-.7.14v10.264c-.608.327-1.168.514-1.635.514-.748 0-.935-.234-1.495-.933l-4.577-7.186v6.952L12.21 19s0 .84-1.168.84l-3.222.186c-.093-.186 0-.653.327-.746l.84-.233V9.854L7.822 9.76c-.094-.42.14-1.026.793-1.073l3.456-.233 4.764 7.279v-6.44l-1.215-.139c-.093-.514.28-.887.747-.933zM1.936 1.035l13.31-.98c1.634-.14 2.055-.047 3.082.7l4.249 2.986c.7.513.934.653.934 1.213v16.378c0 1.026-.373 1.634-1.68 1.726l-15.458.934c-.98.047-1.448-.093-1.962-.747l-3.129-4.06c-.56-.747-.793-1.306-.793-1.96V2.667c0-.839.374-1.54 1.447-1.632z" />
    </Svg>
  );
}

/** Drive's triangle, split the way the real mark splits. Geometry, not a trace. */
export function DriveGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path d="M9.3 2.6h5.4l6.9 12H15L9.3 2.6z" opacity="0.92" />
      <path d="M9.3 2.6 15 14.6H1.4L9.3 2.6z" opacity="0.6" />
      <path d="M1.4 14.6h20.2l-2.6 4.6H4L1.4 14.6z" opacity="0.78" />
    </Svg>
  );
}

export function FigmaGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path d="M12 1H8.5a3.5 3.5 0 0 0 0 7H12V1z" opacity="0.9" />
      <path d="M12 1h3.5a3.5 3.5 0 0 1 0 7H12V1z" opacity="0.62" />
      <path d="M12 8H8.5a3.5 3.5 0 0 0 0 7H12V8z" opacity="0.75" />
      <path d="M15.5 8a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7z" opacity="0.95" />
      <path d="M12 15H8.5A3.5 3.5 0 1 0 12 18.5V15z" opacity="0.55" />
    </Svg>
  );
}

/** Jira's stacked chevrons, reduced to two nested arrows. */
export function JiraGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path d="M11.53 2.06 20.9 11.4a.85.85 0 0 1 0 1.2l-9.37 9.34a.85.85 0 0 1-1.2-1.2l8.16-8.14a.85.85 0 0 0 0-1.2L10.33 3.26a.85.85 0 0 1 1.2-1.2z" />
      <path
        d="M5.4 6.9 10.7 12.2a.6.6 0 0 1 0 .85L5.4 18.35a.6.6 0 0 1-.85-.85l4.45-4.43a.6.6 0 0 0 0-.85L4.55 7.75a.6.6 0 0 1 .85-.85z"
        opacity="0.6"
      />
    </Svg>
  );
}

/** Linear: the mark's diagonal ladder, in a rounded field. */
export function LinearGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <rect
        x="2"
        y="2"
        width="20"
        height="20"
        rx="6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        opacity="0.45"
      />
      <path
        d="M5.6 14.2 9.9 18.5M5.4 10.4l8.3 8.3M6.2 6.9l11 11M9.7 5.4l9 9M13.9 4.9l5.3 5.3"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        fill="none"
      />
    </Svg>
  );
}

/** Confluence: two opposed arcs, the way the real mark reads at small size. */
export function ConfluenceGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path d="M2.2 17.6c2.9-4.6 6.2-6.4 9.8-5.3 2.6.8 4.4 2.5 6.1 4.6l3.3-2.6c-2.4-3-5-5.2-8.4-6.2-5.3-1.6-9.6.9-12.7 6.2l2 3.3z" />
      <path
        d="M21.8 6.4c-2.9 4.6-6.2 6.4-9.8 5.3-2.6-.8-4.4-2.5-6.1-4.6L2.6 9.7c2.4 3 5 5.2 8.4 6.2 5.3 1.6 9.6-.9 12.7-6.2l-1.9-3.3z"
        opacity="0.55"
      />
    </Svg>
  );
}

export function WebGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path
        d="M12 2.6a9.4 9.4 0 1 0 0 18.8 9.4 9.4 0 0 0 0-18.8zm0 0c-2.6 2.3-4 5.5-4 9.4s1.4 7.1 4 9.4c2.6-2.3 4-5.5 4-9.4s-1.4-7.1-4-9.4zM3.2 9.2h17.6M3.2 14.8h17.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </Svg>
  );
}

export function UploadGlyph({ className }: GlyphProps) {
  return (
    <Svg className={className}>
      <path
        d="M14 2.8H7.2A2.2 2.2 0 0 0 5 5v14a2.2 2.2 0 0 0 2.2 2.2h9.6A2.2 2.2 0 0 0 19 19V7.8L14 2.8zM14 2.8v5h5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M12 17.4v-6.2M9.4 13.4 12 10.8l2.6 2.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Svg>
  );
}

/**
 * The Atlas mark: two arcs, one per retrieval channel, overlapping where they
 * fuse. The product's whole thesis at 18 pixels.
 */
export function AtlasMark({ className }: GlyphProps) {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      aria-hidden
      className={className ?? "h-[18px] w-[18px]"}
    >
      <circle cx="7.6" cy="10" r="5.4" stroke="currentColor" strokeWidth="1.5" />
      <circle
        cx="12.4"
        cy="10"
        r="5.4"
        stroke="currentColor"
        strokeWidth="1.5"
        opacity="0.55"
      />
    </svg>
  );
}

/* ───────────────────────────────────────────────────────────── registry ── */

export interface SourceMeta {
  kind: SourceKind;
  label: string;
  Glyph: (p: GlyphProps) => React.JSX.Element;
  /** Brand colour, used only as a 10–14% radial behind the tile on hover. */
  glow: string;
  /** Whether a connector implementation actually exists under `api/`. */
  live: boolean;
  blurb: string;
}

export const SOURCES: Record<SourceKind, SourceMeta> = {
  gdrive: {
    kind: "gdrive",
    label: "Drive",
    Glyph: DriveGlyph,
    glow: "#4285F4",
    live: true,
    blurb: "Docs, Sheets and PDFs from shared drives.",
  },
  notion: {
    kind: "notion",
    label: "Notion",
    Glyph: NotionGlyph,
    glow: "#8f8b84",
    live: true,
    blurb: "Pages and databases, with block structure kept intact.",
  },
  slack: {
    kind: "slack",
    label: "Slack",
    Glyph: SlackGlyph,
    glow: "#7a3f7d",
    live: true,
    blurb: "Public channels, chunked by thread rather than by message.",
  },
  github: {
    kind: "github",
    label: "GitHub",
    Glyph: GitHubGlyph,
    glow: "#8f8b84",
    live: true,
    blurb: "Code and Markdown, chunked by symbol and by heading.",
  },
  jira: {
    kind: "jira",
    label: "Jira",
    Glyph: JiraGlyph,
    glow: "#0052CC",
    live: true,
    blurb: "Issues, comments and resolution history.",
  },
  linear: {
    kind: "linear",
    label: "Linear",
    Glyph: LinearGlyph,
    glow: "#5E6AD2",
    live: false,
    blurb: "Issues and project documents.",
  },
  confluence: {
    kind: "confluence",
    label: "Confluence",
    Glyph: ConfluenceGlyph,
    glow: "#2684FF",
    live: false,
    blurb: "Spaces and whole page trees.",
  },
  figma: {
    kind: "figma",
    label: "Figma",
    Glyph: FigmaGlyph,
    glow: "#F24E1E",
    live: false,
    blurb: "File descriptions and comment threads.",
  },
  web: {
    kind: "web",
    label: "Web",
    Glyph: WebGlyph,
    glow: "#8f8b84",
    live: true,
    blurb: "Any public URL list — docs sites, changelogs, status pages.",
  },
  upload: {
    kind: "upload",
    label: "Upload",
    Glyph: UploadGlyph,
    glow: "#8f8b84",
    live: true,
    blurb: "PDF, DOCX, Markdown, HTML and source files you drop in.",
  },
};

/** Falls back to the generic file glyph for kinds the UI does not know yet. */
export function sourceMeta(kind: string): SourceMeta {
  return SOURCES[kind as SourceKind] ?? SOURCES.upload;
}

/** The belt on the landing page, in the order they read best. */
export const BELT: SourceKind[] = [
  "gdrive",
  "notion",
  "slack",
  "github",
  "jira",
  "linear",
  "confluence",
  "figma",
];
