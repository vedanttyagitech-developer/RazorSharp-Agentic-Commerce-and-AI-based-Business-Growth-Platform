/**
 * What moved between the version the buyer approved and the version that is current.
 *
 * Every number in this table came off the wire. The only arithmetic here is
 * `deltaMinor`, a subtraction of two integers the server sent, and it exists so the row
 * can say "₹12.00 more" instead of making a person hold two figures in their head. No
 * price is re-derived, no total is recomputed, and a path this component does not
 * recognise is printed as it arrived rather than guessed at.
 *
 * The field path is shown twice on purpose: once in English, so the buyer knows what
 * changed, and once verbatim in monospace, so an engineer reading over their shoulder
 * can match the row to the kernel's audit record.
 */
"use client";

import type { ReactNode } from "react";

import { cx } from "@/components/ui";
import { deltaMinor, formatDelta, formatMinor } from "@/lib/money";
import type { Delta } from "@/lib/api/types";

/** `lines[AMUL-DAIRY-001].unit_price_minor` -> container, key, leaf. */
const INDEXED = /^([A-Za-z_][A-Za-z0-9_]*)\[([^\]]+)\]\.?(.*)$/;

/** The leaves the API actually emits, said in English. */
const LEAF_LABELS: Readonly<Record<string, string>> = {
  total: "order total",
  total_minor: "order total",
  amount_minor: "amount",
  unit_price_minor: "unit price",
  unit_price: "unit price",
  subtotal_minor: "subtotal",
  unit_minor: "unit price",
  line_minor: "line total",
  discount_minor: "discount",
  items_subtotal_minor: "items subtotal",
  tax_minor: "tax",
  items_tax_minor: "items tax",
  tax_bp: "tax rate",
  delivery_fee_minor: "delivery fee",
  delivery_tax_minor: "delivery tax",
  quantity: "quantity",
  stock_units: "units in stock",
  is_available: "availability",
  is_listed: "listing",
  currency: "currency",
  line_items: "items in this order",
  lines: "items in this order",
  content_hash: "content hash",
  catalogue_revision: "catalogue revision",
};

function humaniseLeaf(leaf: string): string {
  const known = LEAF_LABELS[leaf];
  if (known) return known;
  return leaf.replace(/_minor$/, "").replace(/_/g, " ");
}

/**
 * The field path as a sentence fragment.
 *
 * `names` maps a SKU to the display name the server sent on the quote. When there is no
 * such name the SKU is printed: a product invented to fill the gap would be a product
 * the buyer never approved.
 */
export function readableField(path: string, names: Readonly<Record<string, string>> = {}): string {
  const indexed = INDEXED.exec(path);
  if (indexed) {
    const [, , key, leaf] = indexed;
    const subject = names[key] ?? key;
    return leaf ? `${subject} — ${humaniseLeaf(leaf)}` : subject;
  }
  const segments = path.split(".").filter(Boolean);
  if (segments.length === 0) return path;
  const leaf = segments[segments.length - 1];
  const label = humaniseLeaf(leaf);
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/** True when both sides of this row are integers in minor units. */
function isMoneyPath(path: string): boolean {
  if (/_minor$/.test(path)) return true;
  const leaf = path.split(".").pop() ?? path;
  return leaf === "total" || leaf === "amount" || leaf === "subtotal";
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** True for a value that renders as a list rather than as one readable token. */
function isComplex(value: unknown): boolean {
  return Array.isArray(value) || isPlainObject(value);
}

/**
 * A non-money value, rendered as it arrived.
 *
 * The one shaping applied is to a flat object of counts, which is how `line_items`
 * arrives: `{"AMUL-DAIRY-001": 2}` reads far better as a list than as JSON, and turning
 * one into the other adds nothing and drops nothing.
 */
function describeValue(value: unknown, names: Readonly<Record<string, string>>): ReactNode {
  if (value === null || value === undefined) return <span className="text-[var(--ink-5)]">not set</span>;
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number" || typeof value === "string") return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-[var(--ink-5)]">empty</span>;
    return value.map((entry) => String(entry)).join(", ");
  }
  if (isPlainObject(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) return <span className="text-[var(--ink-5)]">nothing</span>;
    return (
      <ul className="space-y-0.5">
        {entries.map(([key, count]) => (
          <li key={key} className="text-[13px]">
            <span className="text-[var(--ink-2)]">{names[key] ?? key}</span>
            {typeof count === "number" || typeof count === "string" ? (
              <span className="tnum text-[var(--ink-4)]"> × {String(count)}</span>
            ) : null}
          </li>
        ))}
      </ul>
    );
  }
  return JSON.stringify(value);
}

function MoneyCell({ minor, currency, struck }: { minor: number; currency: string; struck?: boolean }) {
  return (
    <span
      className={cx(
        "tnum text-[14px] font-semibold",
        struck ? "text-[var(--ink-5)] line-through decoration-[var(--red)] decoration-2" : "text-[var(--ink)]",
      )}
    >
      {formatMinor(minor, currency)}
    </span>
  );
}

function DeltaRow({
  delta,
  currency,
  names,
}: {
  delta: Delta;
  currency: string;
  names: Readonly<Record<string, string>>;
}) {
  const money =
    isMoneyPath(delta.field_path) &&
    typeof delta.approved === "number" &&
    typeof delta.current === "number" &&
    Number.isFinite(delta.approved) &&
    Number.isFinite(delta.current);

  const difference = money ? deltaMinor(delta.approved as number, delta.current as number) : null;

  return (
    <tr className="border-t border-[var(--card-line)] align-top">
      <th scope="row" className="py-3 pr-4 text-left font-normal">
        <span className="block text-[13px] font-semibold text-[var(--ink)]">
          {readableField(delta.field_path, names)}
        </span>
        <code className="mt-0.5 block font-mono text-[11px] break-all text-[var(--ink-5)]">
          {delta.field_path}
        </code>
      </th>
      <td className="py-3 pr-4">
        {money ? (
          <MoneyCell minor={delta.approved as number} currency={currency} struck />
        ) : (
          // A struck-through list reads as one deleted blob; a whole set that was
          // replaced is shown muted instead, and the "It is now" column carries the news.
          <span
            className={cx(
              "text-[13px] text-[var(--ink-4)]",
              isComplex(delta.approved) ? "" : "line-through decoration-[var(--red)]",
            )}
          >
            {describeValue(delta.approved, names)}
          </span>
        )}
      </td>
      <td className="py-3 pr-4">
        {money ? (
          <MoneyCell minor={delta.current as number} currency={currency} />
        ) : (
          <span className="text-[13px] text-[var(--ink)]">{describeValue(delta.current, names)}</span>
        )}
      </td>
      <td className="py-3 text-right">
        {difference === null ? (
          <span className="text-[12px] text-[var(--ink-5)]">changed</span>
        ) : (
          <span
            className={cx(
              "tnum text-[14px] font-bold",
              difference > 0 ? "text-[var(--red)]" : difference < 0 ? "text-[var(--green)]" : "text-[var(--ink-4)]",
            )}
          >
            {formatDelta(difference, currency)}
          </span>
        )}
        {delta.reason ? (
          <code className="mt-0.5 block font-mono text-[11px] text-[var(--ink-5)]">{delta.reason}</code>
        ) : null}
      </td>
    </tr>
  );
}

/**
 * The deltas as a table.
 *
 * A real `<table>` rather than a grid of divs, because the relationship between "what
 * you approved" and "what it is now" is the content, and a screen reader should be able
 * to announce the column with the cell.
 */
export function DeltaTable({
  deltas,
  currency = "INR",
  names = {},
  caption = "What changed between the version you approved and the version that is current",
  className,
}: {
  deltas: readonly Delta[];
  currency?: string;
  names?: Readonly<Record<string, string>>;
  caption?: string;
  className?: string;
}) {
  if (deltas.length === 0) {
    return (
      <p className={cx("text-[13px] text-[var(--ink-4)]", className)}>
        The server listed no field-level differences for this decision.
      </p>
    );
  }

  return (
    /*
     * The scroller is focusable, and that is not decoration. The table is `min-w-[520px]`
     * and the storefront's smallest supported width is 390, so on a phone this container
     * always scrolls — and a scrolling container that cannot take focus is one a keyboard
     * user cannot scroll. What they would be unable to reach is the "It is now" column:
     * the evidence for the refusal, on the screen whose whole job is to present it. An
     * axe-core audit reports this as `scrollable-region-focusable`; it is a real defect
     * rather than a lint, and it was found on the refusal screen at 390px.
     *
     * `role="region"` with a name is what makes the tab stop explicable when a screen
     * reader lands on it, rather than an unlabelled thing that swallows a Tab press. The
     * name is its own sentence rather than a copy of the caption: the refusal card wraps
     * this table in a section already called "What changed", and a region nested inside
     * it under a longer name beginning with the same three words is read out as two
     * almost-identical landmarks in a row.
     */
    <div
      role="region"
      aria-label="The changed fields, as a table that scrolls sideways"
      tabIndex={0}
      className={cx("-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0", className)}
    >
      <table className="w-full min-w-[520px] border-collapse text-left">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="text-[11px] font-bold tracking-[0.04em] text-[var(--ink-4)] uppercase">
            <th scope="col" className="pb-2 pr-4">
              Field
            </th>
            <th scope="col" className="pb-2 pr-4">
              You approved
            </th>
            <th scope="col" className="pb-2 pr-4">
              It is now
            </th>
            <th scope="col" className="pb-2 text-right">
              Difference
            </th>
          </tr>
        </thead>
        <tbody>
          {deltas.map((delta, index) => (
            <DeltaRow
              key={`${delta.field_path}:${index}`}
              delta={delta}
              currency={currency}
              names={names}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
