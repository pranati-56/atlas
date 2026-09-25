import type { Metadata, Viewport } from "next";
import { Instrument_Sans, JetBrains_Mono, Newsreader } from "next/font/google";
import { AppShell } from "@/components/AppShell";
import "./globals.css";

/**
 * Three faces, three jobs.
 *
 * Instrument Sans runs the interface. JetBrains Mono takes anything numeric —
 * scores, latencies, chunk counts sit in columns that have to be scannable, and
 * proportional digits make the eye re-find the decimal point on every row.
 * Newsreader Italic appears exactly once per heading, on one word, and nowhere
 * in body copy: it is a warm note against the sans, not a second voice.
 */
const sans = Instrument_Sans({
  subsets: ["latin"],
  variable: "--font-instrument",
  display: "swap",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
  display: "swap",
  weight: ["400", "500"],
});

const serif = Newsreader({
  subsets: ["latin"],
  variable: "--font-newsreader",
  display: "swap",
  style: "italic",
  weight: ["500"],
});

export const metadata: Metadata = {
  title: "Atlas — ask your company anything",
  description:
    "Atlas connects Drive, Notion, Slack and GitHub, and answers questions with citations you can check — filtered by what each person is allowed to see.",
};

export const viewport: Viewport = {
  themeColor: "#060605",
  colorScheme: "dark",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      className={`${sans.variable} ${mono.variable} ${serif.variable}`}
    >
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
