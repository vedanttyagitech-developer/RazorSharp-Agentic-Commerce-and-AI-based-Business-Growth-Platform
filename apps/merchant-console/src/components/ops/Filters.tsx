"use client";

/**
 * A state filter, with the count for that state beside it.
 *
 * The count comes from the collection's `counts` map, which the API computes across the
 * whole scope rather than over the page -- so a filter that would return nothing says so
 * before it is pressed. The tone dot repeats what the label already says; the label is
 * what carries the meaning, and the dot is only there to make a long row scannable.
 */
import { formatCount } from "@/lib/money";
import { cx, type Tone } from "@/components/ui";

const DOT: Record<Tone, string> = {
  ink: "bg-[var(--ink)]",
  muted: "bg-[var(--faint)]",
  positive: "bg-[var(--positive)]",
  warn: "bg-[var(--warn)]",
  danger: "bg-[var(--danger)]",
  info: "bg-[var(--info)]",
};

export function FilterChip({
  active,
  onClick,
  label,
  count,
  tone,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count?: number;
  tone?: Tone;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cx(
        "mono inline-flex items-center gap-1.5 rounded-[var(--r-sm)] border px-2 py-1 transition-colors",
        active
          ? "border-[var(--info)] bg-[color-mix(in_srgb,var(--info)_16%,transparent)] text-[var(--ink)]"
          : "border-[var(--line)] bg-[var(--raised)] text-[var(--muted)] hover:border-[var(--faint)]",
      )}
    >
      {tone && <span aria-hidden="true" className={cx("size-1.5 rounded-full", DOT[tone])} />}
      <span>{label}</span>
      {count !== undefined && (
        <span className={count === 0 ? "text-[var(--faint)]" : "text-[var(--ink)]"}>
          {formatCount(count)}
        </span>
      )}
    </button>
  );
}
