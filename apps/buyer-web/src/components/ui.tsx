/**
 * The shared primitives. Every screen imports these rather than restyling its own.
 *
 * Small on purpose: enough to keep spacing, radii and state rendering identical across
 * the storefront, and no more. The values come from `docs/BLINKIT_DESIGN_SPEC.md`.
 */
"use client";

import type { ReactNode } from "react";

import { formatMinor, formatMoney, type Money } from "@/lib/money";

function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/* ------------------------------------------------------------------ money */

/**
 * An amount, rendered from integer minor units the server sent.
 *
 * Deliberately has no arithmetic in it and takes no operands: if a screen needs a total,
 * it asks the API for one. Tabular figures so a column of prices lines up.
 */
export function Amount({
  minor,
  currency = "INR",
  money,
  whole = false,
  className,
}: {
  minor?: number;
  currency?: string;
  money?: Money | null;
  whole?: boolean;
  className?: string;
}) {
  const text = money ? formatMoney(money, { whole }) : formatMinor(minor ?? 0, currency, { whole });
  return <span className={cx("tnum", className)}>{text}</span>;
}

/* ---------------------------------------------------------------- controls */

type ButtonProps = {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  busy?: boolean;
  variant?: "primary" | "outline" | "ghost" | "danger";
  size?: "sm" | "md" | "lg";
  type?: "button" | "submit";
  className?: string;
  "aria-label"?: string;
};

const VARIANTS: Record<NonNullable<ButtonProps["variant"]>, string> = {
  primary: "bg-[var(--green)] text-white hover:brightness-95",
  outline: "border border-[var(--green-add)] bg-[var(--green-add-bg)] text-[var(--green-add)] hover:brightness-98",
  ghost: "border border-[var(--card-line)] bg-white text-[var(--ink)] hover:bg-[var(--tint-2)]",
  danger: "border border-[var(--red)] bg-white text-[var(--red)] hover:bg-red-50",
};

const SIZES: Record<NonNullable<ButtonProps["size"]>, string> = {
  sm: "h-8 px-3 text-[13px] rounded-[var(--r-sm)]",
  md: "h-10 px-4 text-[14px] rounded-[var(--r-md)]",
  lg: "h-12 px-6 text-[16px] rounded-[var(--r-md)]",
};

export function Button({
  children,
  onClick,
  disabled,
  busy,
  variant = "primary",
  size = "md",
  type = "button",
  className,
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={cx(
        "inline-flex items-center justify-center gap-2 font-semibold transition disabled:cursor-not-allowed disabled:opacity-50",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {busy ? <Spinner /> : null}
      {children}
    </button>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cx(
        "inline-block h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent",
        className,
      )}
    />
  );
}

/* ------------------------------------------------------------------ badges */

const TONES = {
  neutral: "bg-[var(--tint-1)] text-[var(--ink-3)]",
  green: "bg-[var(--green-add-bg)] text-[var(--green-add)] border border-[var(--green-add)]",
  blue: "bg-blue-50 text-[var(--blue)]",
  amber: "bg-amber-50 text-[var(--amber)]",
  red: "bg-red-50 text-[var(--red)]",
} as const;

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: keyof typeof TONES;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/* -------------------------------------------------------------- containers */

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cx(
        "rounded-[var(--r-md)] border-[0.5px] border-[var(--card-line)] bg-white",
        className,
      )}
      style={{ boxShadow: "var(--card-shadow)" }}
    >
      {children}
    </div>
  );
}

/* ------------------------------------------------------------- data states */

export function Skeleton({ className }: { className?: string }) {
  return <div className={cx("animate-pulse rounded-[var(--r-md)] bg-[var(--tint-1)]", className)} />;
}

/**
 * What a screen shows when a read failed.
 *
 * It says what went wrong and offers a retry. It never substitutes invented data: a
 * storefront that quietly falls back to fixtures shows a buyer a price the kernel never
 * agreed to, and on a payments submission that is worse than an honest error.
 */
export function ErrorState({
  title = "Something went wrong",
  detail,
  onRetry,
}: {
  title?: string;
  detail?: string;
  onRetry?: () => void;
}) {
  return (
    <div role="alert" className="flex flex-col items-center gap-3 px-6 py-16 text-center">
      <p className="text-[16px] font-semibold text-[var(--ink)]">{title}</p>
      {detail ? <p className="max-w-md text-[13px] text-[var(--ink-4)]">{detail}</p> : null}
      {onRetry ? (
        <Button variant="ghost" size="sm" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-16 text-center">
      <p className="text-[16px] font-semibold text-[var(--ink)]">{title}</p>
      {detail ? <p className="max-w-md text-[13px] text-[var(--ink-4)]">{detail}</p> : null}
      {action}
    </div>
  );
}

export { cx };
