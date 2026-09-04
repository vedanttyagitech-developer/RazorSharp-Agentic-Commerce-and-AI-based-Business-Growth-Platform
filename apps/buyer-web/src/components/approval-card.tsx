"use client";

import type { ApprovalCard as ApprovalCardData, ApprovalEcho, ApprovalRecord, Delta } from "@/lib/api/types";
import { formatMinor } from "@/lib/money";

import { QuoteBreakdown } from "./quote-breakdown";
import { Button, Countdown, DefinitionList, MonoValue, StatusPill } from "./ui";

export interface ApprovalCardProps {
  card: ApprovalCardData;
  onApprove: (echo: ApprovalEcho) => void | Promise<void>;
  onReject?: () => void | Promise<void>;
  busy?: boolean;
  /** When present the card renders as evidence of an approval already given. */
  approved?: ApprovalRecord | null;
}

/**
 * The trusted approval card (spec 4.3, 10.2). Everything shown is server-confirmed. The
 * approve action submits exactly the content hash, amount and currency displayed here --
 * the object is built from the same props that rendered the text, with no other fields.
 */
export function ApprovalCard({ card, onApprove, onReject, busy = false, approved = null }: ApprovalCardProps) {
  const total = formatMinor(card.amount_minor, card.currency);
  const echo: ApprovalEcho = { content_hash: card.content_hash, amount_minor: card.amount_minor, currency: card.currency };

  return (
    <article aria-labelledby={`approval-${card.checkout_id}-${card.version}`} className="space-y-4 rounded-lg border-2 border-accent bg-surface p-4 shadow-sm" data-testid="approval-card">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 id={`approval-${card.checkout_id}-${card.version}`} className="text-lg font-semibold">
            {approved ? `Approved: version ${card.version}` : `Approve version ${card.version}`}
          </h2>
          <p className="text-sm text-muted">Trusted buyer surface. Not an LLM tool; rendered from server-confirmed data.</p>
        </div>
        {approved ? <StatusPill tone="success" glyph="✓" label="Approval recorded" /> : <StatusPill tone="warning" glyph="?" label="Awaiting your approval" />}
      </header>

      <DefinitionList
        items={[
          { term: "Checkout", detail: <span className="font-mono text-xs" data-testid="approval-checkout-id">{card.checkout_id}</span> },
          { term: "Version", detail: <span data-testid="approval-version">{card.version}</span> },
          { term: "Content hash (JCS SHA-256)", detail: <span className="break-all" data-testid="approval-content-hash"><MonoValue value={card.content_hash} label="content hash" /></span> },
          { term: "Policy-at-Sale receipt", detail: <span className="break-all" data-testid="approval-receipt-hash"><MonoValue value={card.policy_receipt_hash} label="policy receipt hash" /> <span className="text-xs text-muted break-all">({card.policy_receipt_id})</span></span> },
          { term: "Total", detail: <strong className="text-base tabular-nums" data-testid="approval-total">{total}</strong> },
          { term: "Currency", detail: <span data-testid="approval-currency">{card.currency}</span> },
          { term: "Approval window", detail: <Countdown expiresAt={card.expires_at} label="expires in" /> },
          ...(card.reservation
            ? [{ term: "Reservation", detail: <span><span className="font-mono text-xs">{card.reservation.reservation_id}</span> · {card.reservation.state} · <Countdown expiresAt={card.reservation.expires_at} label="held for" /></span> }]
            : []),
          ...(approved
            ? [{ term: "Approval id", detail: <span className="font-mono text-xs">{approved.approval_id} · epoch {approved.authority_epoch}</span> }]
            : []),
        ]}
      />

      <details className="rounded-md border border-line p-3" open={!approved}>
        <summary className="cursor-pointer text-sm font-medium">Exact items, fees and taxes for version {card.version}</summary>
        <div className="mt-3">
          <QuoteBreakdown quote={card.quote} showDeliveryGap={!approved} />
        </div>
      </details>

      {!approved ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => onApprove(echo)} busy={busy} data-testid="approve-button">
            Approve {total} for version {card.version}
          </Button>
          {onReject ? (
            <Button variant="secondary" onClick={() => onReject()} disabled={busy} data-testid="reject-button">
              Reject and release reservation
            </Button>
          ) : null}
          <p className="basis-full text-xs text-muted">
            Approving submits exactly this content hash, amount and currency. Any material change invalidates the approval and produces a new version.
          </p>
        </div>
      ) : null}
    </article>
  );
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

/** Delta card (spec 4.1): exact field-level differences between the approved and current state. */
export function DeltaView({ deltas, invalidatedVersion, nextVersion, currency }: { deltas: Delta[]; invalidatedVersion: number; nextVersion: number; currency: string }) {
  const isMoney = (path: string) => path.endsWith("_minor");
  return (
    <section aria-labelledby="delta-heading" className="space-y-3 rounded-lg border-2 border-rose-400 bg-surface p-4" data-testid="delta-view">
      <h2 id="delta-heading" className="text-lg font-semibold">
        <span aria-hidden="true" className="font-mono">Δ </span>Material delta: version {invalidatedVersion} is invalidated; approve version {nextVersion}
      </h2>
      <p className="text-sm">
        Merchant state changed underneath your approval. The kernel refused to create a payment for version {invalidatedVersion}. Your earlier approval cannot be reused: it was bound to the old hash and total. Review the exact differences and approve version {nextVersion} if you still want it.
      </p>
      <div className="overflow-x-auto -mx-1 sm:mx-0">
        <table className="w-full min-w-[340px] text-sm">
          <caption className="sr-only">Field-level differences between the approved version and current merchant state</caption>
          <thead>
            <tr className="border-b border-line text-left text-xs text-muted">
              <th scope="col" className="py-1 pr-2">field_path</th>
              <th scope="col" className="py-1 pr-2">approved (v{invalidatedVersion})</th>
              <th scope="col" className="py-1 pr-2">current (v{nextVersion})</th>
              <th scope="col" className="py-1">reason</th>
            </tr>
          </thead>
          <tbody>
            {deltas.map((delta) => (
              <tr key={delta.field_path} className="border-b border-line/60 align-top">
                <td className="py-1 pr-2 font-mono text-xs">{delta.field_path}</td>
                <td className="py-1 pr-2 tabular-nums">
                  {renderValue(delta.approved)}
                  {isMoney(delta.field_path) && typeof delta.approved === "number" ? <span className="block text-xs text-muted">{formatMinor(delta.approved, currency)}</span> : null}
                </td>
                <td className="py-1 pr-2 font-semibold tabular-nums">
                  {renderValue(delta.current)}
                  {isMoney(delta.field_path) && typeof delta.current === "number" ? <span className="block text-xs text-muted">{formatMinor(delta.current, currency)}</span> : null}
                </td>
                <td className="py-1 font-mono text-xs">{delta.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
