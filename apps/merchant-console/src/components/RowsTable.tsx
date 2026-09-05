"use client";

/**
 * A table over rows the API declares as `dict[str, Any]`.
 *
 * The inspector's blocks are deliberately untyped on the server: a strict model per block
 * would silently drop a column the day the schema gains one, and the value of a forensic
 * document is completeness. This component keeps that property on the client. It renders
 * the union of every key present across the rows, in the order the keys first appear, so a
 * column the console has never heard of still arrives on screen with its name attached.
 *
 * Values are formatted by shape, never reinterpreted: a timestamp key renders as a time, a
 * boolean as a labelled flag, a state as a chip, and anything structured as the JSON it
 * is. `null` renders as an em dash and never as zero or false.
 */
import type { ReactNode } from "react";
import { Chip, Empty, TableWrap, Td, Th, When, toneForState } from "./ui";

const TIME_KEYS = /_at$|_until$|^next_scheduled_attempt$/;
const STATE_KEYS = /^(state|status|apply_status|row_status|outcome_code|code|decision)$/;
const ID_LINK: Readonly<Record<string, (value: string) => string>> = {
  payment_attempt_id: (value) => `/inspector?attempt=${encodeURIComponent(value)}`,
};

export function RowsTable({
  rows,
  empty,
  emphasise = [],
}: {
  rows: Array<Record<string, unknown>>;
  empty: ReactNode;
  /** Keys pulled to the front, because they are what the block is read for. */
  emphasise?: string[];
}) {
  if (rows.length === 0) return <Empty>{empty}</Empty>;

  const seen: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) if (!seen.includes(key)) seen.push(key);
  }
  const columns = [
    ...emphasise.filter((key) => seen.includes(key)),
    ...seen.filter((key) => !emphasise.includes(key)),
  ];

  return (
    <TableWrap>
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr>
            {columns.map((key) => (
              <Th key={key}>{key}</Th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} className="hover:bg-[var(--raised)]">
              {columns.map((key) => (
                <Td key={key}>
                  <Cell name={key} value={row[key]} />
                </Td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}

function Cell({ name, value }: { name: string; value: unknown }) {
  if (value === null || value === undefined) return <span className="mono text-[var(--faint)]">—</span>;

  if (typeof value === "boolean") {
    return <Chip tone={value ? "positive" : "muted"}>{String(value)}</Chip>;
  }

  if (typeof value === "string") {
    if (TIME_KEYS.test(name)) return <When value={value} />;
    if (STATE_KEYS.test(name)) return <Chip tone={toneForState(value)}>{value}</Chip>;
    const link = ID_LINK[name];
    if (link) {
      return (
        <a href={link(value)} title={value} className="mono text-[var(--info)] hover:underline break-id">
          {value}
        </a>
      );
    }
    return <span className="mono text-[var(--ink)] break-id">{value}</span>;
  }

  if (typeof value === "number") {
    return <span className="num text-[var(--ink)]">{value}</span>;
  }

  return (
    <pre className="mono max-w-[420px] overflow-x-auto rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--bg)] p-2 text-[var(--muted)]">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}
