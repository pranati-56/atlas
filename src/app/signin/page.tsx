"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { AtlasMark, BELT, SOURCES } from "@/components/icons";
import { ApiError, api, type AuthConfig, type WorkspaceChoice } from "@/lib/api";
import { auth } from "@/lib/auth";

type Mode = "signin" | "signup";

export default function SignInRoute() {
  // useSearchParams needs a suspense boundary or the route cannot prerender.
  return (
    <Suspense fallback={null}>
      <SignIn />
    </Suspense>
  );
}

function SignIn() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/ask";

  const [mode, setMode] = useState<Mode>("signin");
  const [config, setConfig] = useState<AuthConfig | null>(null);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [name, setName] = useState("");

  const [choices, setChoices] = useState<WorkspaceChoice[] | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const emailField = useRef<HTMLInputElement>(null);

  useEffect(() => {
    void api
      .authConfig()
      .then(setConfig)
      .catch(() => setConfig(null));
    emailField.current?.focus();
  }, []);

  // Already signed in? Nobody should have to sign in twice.
  useEffect(() => {
    void auth.refresh().then(() => {
      if (auth.snapshot().status === "in") router.replace(next);
    });
  }, [router, next]);

  const submit = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError(null);

    try {
      if (mode === "signup") {
        const res = await api.signup({
          workspace: workspace.trim(),
          email: email.trim(),
          password,
          ...(name.trim() ? { name: name.trim() } : {}),
        });
        auth.adopt(res.user);
        router.replace(next);
        return;
      }

      const res = await api.login({
        email: email.trim(),
        password,
        ...(chosen ? { workspace: chosen } : {}),
      });

      if (res.ok) {
        auth.adopt(res.user);
        router.replace(next);
        return;
      }

      // The address belongs to more than one workspace. Ask, never guess.
      setChoices(res.choices);
      setChosen(res.choices[0]?.slug ?? null);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Something went wrong. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }, [busy, mode, workspace, email, password, name, chosen, router, next]);

  const min = config?.min_password ?? 10;
  const ready =
    mode === "signup"
      ? workspace.trim().length >= 2 &&
        email.trim().length > 3 &&
        password.length >= min
      : email.trim().length > 3 && password.length > 0;

  return (
    // overflow-hidden because the glow below is wider than the column it is
    // anchored to and would otherwise add a horizontal scrollbar.
    <div className="relative flex min-h-screen overflow-hidden">
      <div
        aria-hidden
        className="pointer-events-none absolute left-1/4 top-0 h-[38rem] w-[46rem] -translate-x-1/2 -translate-y-1/3"
        style={{
          background:
            "radial-gradient(50% 50% at 50% 50%, rgba(255,247,232,0.09), transparent 70%)",
        }}
      />

      <div className="relative z-10 flex w-full flex-col justify-center px-6 py-12 lg:w-[52%] lg:px-16">
        <div className="mx-auto w-full max-w-[24rem]">
          <Link href="/" className="mb-12 flex items-center gap-2.5">
            <AtlasMark className="h-[19px] w-[19px] text-ink" />
            <span className="text-[0.95rem] font-semibold tracking-[-0.025em]">
              Atlas
            </span>
          </Link>

          <h1 className="display text-[2.4rem] text-ink">
            {mode === "signin" ? (
              <>
                Welcome <span className="accent">back</span>.
              </>
            ) : (
              <>
                Start a <span className="accent">workspace</span>.
              </>
            )}
          </h1>
          <p className="mt-3 text-sm leading-relaxed text-ink-dim">
            {mode === "signin"
              ? "Answers are filtered by what you are allowed to see, so who you are matters here."
              : "You will be its first admin. Connect a source and it fills itself in."}
          </p>

          {config?.dev_user && <DevBanner user={config.dev_user} />}

          <form
            className="mt-8 flex flex-col gap-3.5"
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            {mode === "signup" && (
              <Field
                label="Workspace name"
                value={workspace}
                onChange={setWorkspace}
                placeholder="Acme"
                autoComplete="organization"
              />
            )}

            <Field
              ref={emailField}
              label="Email"
              type="email"
              value={email}
              onChange={(v) => {
                setEmail(v);
                // The address changed, so a workspace chosen for the old one is
                // meaningless.
                setChoices(null);
                setChosen(null);
              }}
              placeholder="you@company.com"
              autoComplete="email"
            />

            {mode === "signup" && (
              <Field
                label="Your name"
                optional
                value={name}
                onChange={setName}
                placeholder="Jane Doe"
                autoComplete="name"
              />
            )}

            <Field
              label="Password"
              type="password"
              value={password}
              onChange={setPassword}
              autoComplete={mode === "signup" ? "new-password" : "current-password"}
              note={
                mode === "signup"
                  ? `At least ${min} characters. Length beats punctuation.`
                  : undefined
              }
            />

            {choices && (
              <WorkspacePicker
                choices={choices}
                chosen={chosen}
                onChoose={setChosen}
              />
            )}

            {error && (
              <p className="rounded-[8px] border border-[rgba(224,115,109,0.28)] bg-[rgba(224,115,109,0.07)] px-3 py-2 text-2xs leading-relaxed text-[#eda6a1]">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={!ready || busy}
              className="btn btn-primary btn-lg mt-1"
            >
              {busy
                ? "One moment…"
                : mode === "signin"
                  ? choices
                    ? "Continue"
                    : "Sign in"
                  : "Create workspace"}
            </button>
          </form>

          {config?.allow_signup !== false && (
            <p className="mt-6 text-xs text-ink-faint">
              {mode === "signin" ? "No workspace yet? " : "Already have one? "}
              <button
                type="button"
                onClick={() => {
                  setMode(mode === "signin" ? "signup" : "signin");
                  setError(null);
                  setChoices(null);
                  setChosen(null);
                }}
                className="text-ink underline decoration-line-strong underline-offset-4 transition-colors hover:decoration-ink-faint"
              >
                {mode === "signin" ? "Create one" : "Sign in"}
              </button>
            </p>
          )}
        </div>
      </div>

      <aside className="relative z-10 hidden border-l border-line lg:flex lg:w-[48%] lg:flex-col lg:justify-center lg:px-16">
        <p className="label">What you get</p>
        <p className="mt-4 max-w-[22rem] text-[1.3rem] leading-[1.45] tracking-[-0.02em] text-ink-dim">
          One question, asked once, answered from{" "}
          <span className="text-ink">everything your company already wrote</span> —
          with the passage attached.
        </p>

        <div className="mt-9 flex flex-wrap gap-2">
          {BELT.map((kind) => {
            const meta = SOURCES[kind];
            return (
              <span
                key={kind}
                title={meta.label}
                className="flex h-10 w-10 items-center justify-center rounded-[10px] border border-line bg-[rgba(255,252,245,0.02)] text-ink-faint"
              >
                <meta.Glyph className="h-4 w-4" />
              </span>
            );
          })}
        </div>

        <p className="mt-6 max-w-[22rem] text-xs leading-relaxed text-ink-ghost">
          Permissions are applied inside the retrieval scan, not filtered
          afterwards. Two people asking the same question get different answers,
          and neither can retrieve what they cannot open.
        </p>
      </aside>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────── controls ── */

function Field({
  ref,
  label,
  value,
  onChange,
  note,
  optional,
  ...props
}: {
  ref?: React.Ref<HTMLInputElement>;
  label: string;
  value: string;
  onChange: (v: string) => void;
  note?: string;
  optional?: boolean;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "ref">) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="flex items-baseline gap-1.5 text-2xs text-ink-faint">
        {label}
        {optional && <span className="text-ink-ghost">optional</span>}
      </span>
      <input
        ref={ref}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="field"
        {...props}
      />
      {note && <span className="text-2xs text-ink-ghost">{note}</span>}
    </label>
  );
}

/**
 * One address, several workspaces.
 *
 * Shown only after the server says the email is ambiguous. Asking everyone for
 * a workspace slug up front would tax the many for the sake of the few.
 */
function WorkspacePicker({
  choices,
  chosen,
  onChoose,
}: {
  choices: WorkspaceChoice[];
  chosen: string | null;
  onChoose: (slug: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-2xs text-ink-faint">
        That address is in {choices.length} workspaces. Which one?
      </span>
      <div className="flex flex-col gap-1">
        {choices.map((c) => (
          <button
            key={c.slug}
            type="button"
            onClick={() => onChoose(c.slug)}
            className={`flex items-center gap-2.5 rounded-[8px] border px-3 py-2 text-left transition-colors duration-150 ${
              chosen === c.slug
                ? "border-line-lit bg-s2 text-ink"
                : "border-line text-ink-dim hover:border-line-strong"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                chosen === c.slug ? "bg-ink" : "bg-ink-ghost"
              }`}
            />
            <span className="text-sm">{c.name}</span>
            <span className="num ml-auto text-2xs text-ink-ghost">{c.slug}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * ATLAS_DEV_USER is on, so the form below is decorative — every request is
 * already authenticated as that address. Saying so beats letting someone wonder
 * why a wrong password still worked.
 */
function DevBanner({ user }: { user: string }) {
  return (
    <div className="mt-6 rounded-[9px] border border-[rgba(224,177,84,0.28)] bg-[rgba(224,177,84,0.07)] px-3 py-2.5">
      <p className="text-2xs leading-relaxed text-warn">
        <span className="font-medium">ATLAS_DEV_USER is set.</span> Every request
        is authenticated as <span className="num">{user}</span> regardless of what
        you type here. Unset it in <span className="num">api/.env</span> to use
        real sign-in.
      </p>
    </div>
  );
}
