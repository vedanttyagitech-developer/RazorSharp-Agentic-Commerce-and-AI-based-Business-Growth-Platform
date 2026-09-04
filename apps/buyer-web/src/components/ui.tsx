"use client";

import { useState, useSyncExternalStore, type ButtonHTMLAttributes, type ReactNode } from "react";

import type { Tone } from "@/lib/journey";
import { shortHash } from "@/lib/hash";

/** Tone classes carry colour only; every use pairs them with a glyph and a text label. */
export const TONE_CLASSES: Record<Tone, string> = {
  neutral: "border-stone-300 bg-stone-100 text-stone-800 dark:border-stone-600 dark:bg-stone-800 dark:text-stone-100",
  info: "border-sky-300 bg-sky-50 text-sky-900 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-100",
  pending: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100",
  success: "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-700 dark:bg-emerald-950 dark:text-emerald-100",
  warning: "border-orange-300 bg-orange-50 text-orange-900 dark:border-orange-700 dark:bg-orange-950 dark:text-orange-100",
  danger: "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-700 dark:bg-rose-950 dark:text-rose-100",
};

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

const BUTTON_CLASSES: Record<ButtonVariant, string> = {
  primary: "bg-accent text-accent-ink hover:opacity-90 border-transparent",
  secondary: "bg-surface text-foreground border-line hover:bg-stone-100 dark:hover:bg-stone-800",
  danger: "bg-rose-700 text-white border-transparent hover:bg-rose-800",
  ghost: "bg-transparent text-foreground border-transparent hover:bg-stone-100 dark:hover:bg-stone-800",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  busy?: boolean;
}

export function Button({ variant = "primary", busy = false, className = "", children, disabled, type = "button", ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      className={`inline-flex min-h-10 items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-60 ${BUTTON_CLASSES[variant]} ${className}`}
      {...rest}
    >
      {busy ? <span aria-hidden="true">…</span> : null}
      {children}
    </button>
  );
}

export interface StatusPillProps {
  tone: Tone;
  glyph: string;
  label: string;
  className?: string;
}

/** Glyph + text label; colour is never the only signal (spec 29.8). */
export function StatusPill({ tone, glyph, label, className = "" }: StatusPillProps) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${TONE_CLASSES[tone]} ${className}`}>
      <span aria-hidden="true" className="font-mono">{glyph}</span>
      <span>{label}</span>
    </span>
  );
}

export function Panel({ title, tone = "neutral", children, actions, id }: { title: ReactNode; tone?: Tone; children: ReactNode; actions?: ReactNode; id?: string }) {
  return (
    <section id={id} className={`rounded-lg border bg-surface p-4 shadow-sm ${tone === "neutral" ? "border-line" : TONE_CLASSES[tone].split(" ")[0]}`}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <h2 className="text-base font-semibold">{title}</h2>
        {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Alert({ tone, title, children, role = "status" }: { tone: Tone; title: string; children?: ReactNode; role?: "status" | "alert" }) {
  return (
    <div role={role} className={`rounded-md border px-3 py-2 text-sm ${TONE_CLASSES[tone]}`}>
      <p className="font-semibold">{title}</p>
      {children ? <div className="mt-1">{children}</div> : null}
    </div>
  );
}

export function DefinitionList({ items }: { items: { term: string; detail: ReactNode }[] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
      {items.map((item) => (
        <div key={item.term} className="contents">
          <dt className="text-muted">{item.term}</dt>
          <dd className="break-all">{item.detail}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Monospace value with a copy button; announces the copy through a live region. */
export function MonoValue({ value, label, short = true }: { value: string; label: string; short?: boolean }) {
  const [message, setMessage] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setMessage(`${label} copied`);
    } catch {
      setMessage(`Could not copy ${label}; select the text instead`);
    }
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <code className="rounded bg-stone-100 px-1.5 py-0.5 font-mono text-xs dark:bg-stone-800" title={value}>
        {short ? shortHash(value) : value}
      </code>
      <button type="button" onClick={copy} className="rounded border border-line px-2 py-0.5 text-xs hover:bg-stone-100 dark:hover:bg-stone-800" aria-label={`Copy ${label}`}>
        Copy
      </button>
      <span role="status" aria-live="polite" className="sr-only">{message}</span>
    </span>
  );
}

function subscribeSecond(callback: () => void): () => void {
  const timer = setInterval(callback, 1000);
  return () => clearInterval(timer);
}

function secondSnapshot(): number {
  return Math.floor(Date.now() / 1000) * 1000;
}

function serverSnapshot(): number {
  return 0;
}

/** Wall clock at one-second resolution. Display only; expiry is decided by the server. */
export function useNow(): number {
  return useSyncExternalStore(subscribeSecond, secondSnapshot, serverSnapshot);
}

export function Countdown({ expiresAt, label }: { expiresAt: string; label: string }) {
  const now = useNow();
  const expires = new Date(expiresAt).getTime();
  if (now === 0 || Number.isNaN(expires)) return <span>{label}: —</span>;
  const remaining = Math.max(0, expires - now);
  const minutes = Math.floor(remaining / 60_000);
  const seconds = Math.floor((remaining % 60_000) / 1000);
  const expired = remaining === 0;
  return (
    <span role="timer" aria-live={expired ? "assertive" : "off"} className={expired ? "font-semibold" : ""}>
      {label}: {expired ? "expired (server decides)" : `${minutes}:${String(seconds).padStart(2, "0")}`}
    </span>
  );
}

export function Spinner({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-muted" role="status">
      <span aria-hidden="true" className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
      {label}
    </span>
  );
}
