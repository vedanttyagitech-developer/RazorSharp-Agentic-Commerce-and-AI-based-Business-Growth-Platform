/**
 * The approval card: the exact bytes the buyer is being asked to consent to.
 *
 * Everything on this card is on it because the approval binds to it. The version number
 * says which immutable document is being approved; the content hash says which bytes
 * that document is; the policy receipt hash says which merchant rules were in force when
 * it was priced; the reservation says how long the stock behind it is held. If any of
 * those move before payment, the kernel refuses the approval rather than charging a
 * different amount, and the buyer should be able to see, before they press anything,
 * exactly which facts their consent is nailed to.
 *
 * The countdown is a display of the server's expiry, never a gate. The button stays
 * pressable after it runs out, with a warning, because the browser does not get to
 * decide whether an approval is still good. The kernel does.
 */
"use client";

import { useEffect, useState } from "react";

import { Amount, Badge, Button, cx } from "@/components/ui";
import { VoiceConsent } from "@/features/voice/voice-consent";
import { formatMinor } from "@/lib/money";
import type { ApprovalCard as ApprovalCardData, Quote } from "@/lib/api/types";

import { DeltaTable } from "./delta-table";
import { TrustedActions, TrustedFact, TrustedSurface } from "./trusted-surface";

/* ------------------------------------------------------------------ hashes */

/**
 * A hash, truncated to the first twelve characters.
 *
 * Truncated because nobody reads sixty-four hex characters, and shown at all because the
 * point of the card is that consent is about bytes. The full value is in the title and
 * in the accessible label, so it can still be copied or read out.
 */
export function HashChip({ value, label }: { value: string | null; label: string }) {
  if (!value) {
    return (
      <span className="text-[13px] text-[var(--ink-5)]" aria-label={`${label}: not set`}>
        not set
      </span>
    );
  }
  const head = value.length > 12 ? `${value.slice(0, 12)}…` : value;
  return (
    <code
      title={value}
      aria-label={`${label}: ${value}`}
      className="tnum rounded-[var(--r-sm)] bg-[var(--tint-1)] px-1.5 py-0.5 font-mono text-[12px] break-all text-[var(--ink-2)]"
    >
      {head}
    </code>
  );
}

/* --------------------------------------------------------------- countdown */

/**
 * Seconds left until an ISO instant, recomputed once a second.
 *
 * Returns `null` until the component has mounted, because the server render and the
 * browser render would otherwise disagree about the time and React would report a
 * hydration mismatch on the most important card in the application.
 */
function useSecondsUntil(iso: string | null): number | null {
  const deadline = iso ? Date.parse(iso) : Number.NaN;
  const usable = Number.isFinite(deadline);
  const [seconds, setSeconds] = useState<number | null>(null);

  useEffect(() => {
    if (!usable) return;
    const tick = () => setSeconds(Math.round((deadline - Date.now()) / 1000));
    // The first reading is scheduled rather than taken inline, so the clock is only ever
    // read in the browser: a value computed during render would differ from the one the
    // server rendered and React would report a hydration mismatch.
    const first = window.setTimeout(tick, 0);
    const timer = window.setInterval(tick, 1000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [deadline, usable]);

  return usable ? seconds : null;
}

function clockText(seconds: number): string {
  const safe = Math.max(0, seconds);
  const minutes = Math.floor(safe / 60);
  const rest = safe % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function ReservationCountdown({ expiresAt }: { expiresAt: string | null }) {
  const seconds = useSecondsUntil(expiresAt);

  if (!expiresAt) {
    return <span className="text-[13px] text-[var(--ink-5)]">no expiry was sent for this hold</span>;
  }
  if (seconds === null) {
    return <span className="text-[13px] text-[var(--ink-4)]">reading the clock…</span>;
  }
  if (seconds <= 0) {
    return (
      <span className="text-[13px] font-semibold text-[var(--red)]" aria-live="polite">
        The hold has expired. You can still approve; the kernel decides whether it stands.
      </span>
    );
  }

  const urgent = seconds <= 60;
  return (
    <span aria-live="polite" className="inline-flex items-baseline gap-2">
      <span
        className={cx(
          "tnum text-[16px] font-bold",
          urgent ? "text-[var(--red)]" : "text-[var(--ink)]",
        )}
      >
        {clockText(seconds)}
      </span>
      <span className="text-[12px] text-[var(--ink-4)]">
        left on this hold{urgent ? " — it is about to be released" : ""}
      </span>
    </span>
  );
}

/* ------------------------------------------------------------------- quote */

function QuoteBreakdown({ quote }: { quote: Quote }) {
  const rows: Array<{ label: string; minor: number; muted?: boolean }> = [
    { label: "Items", minor: quote.items_subtotal_minor },
    { label: "Tax on items", minor: quote.items_tax_minor, muted: true },
    { label: "Delivery", minor: quote.delivery_fee_minor },
    { label: "Tax on delivery", minor: quote.delivery_tax_minor, muted: true },
  ];

  return (
    <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
      <table className="w-full min-w-[460px] border-collapse text-left">
        <caption className="sr-only">The priced lines this approval covers</caption>
        <thead>
          <tr className="text-[11px] font-bold tracking-[0.04em] text-[var(--ink-4)] uppercase">
            <th scope="col" className="pb-2 pr-4">
              Item
            </th>
            <th scope="col" className="pb-2 pr-4 text-right">
              Qty
            </th>
            <th scope="col" className="pb-2 pr-4 text-right">
              Unit
            </th>
            <th scope="col" className="pb-2 pr-4 text-right">
              Tax
            </th>
            <th scope="col" className="pb-2 text-right">
              Amount
            </th>
          </tr>
        </thead>
        <tbody>
          {quote.lines.map((line) => (
            <tr key={line.sku} className="border-t border-[var(--card-line)]">
              <th scope="row" className="py-2.5 pr-4 text-left font-normal">
                <span className="block text-[13px] font-semibold text-[var(--ink)]">{line.name}</span>
                <code className="font-mono text-[11px] text-[var(--ink-5)]">{line.sku}</code>
              </th>
              <td className="tnum py-2.5 pr-4 text-right text-[13px] text-[var(--ink-2)]">{line.quantity}</td>
              <td className="tnum py-2.5 pr-4 text-right text-[13px] text-[var(--ink-2)]">
                {formatMinor(line.unit_price_minor, quote.currency)}
              </td>
              <td className="tnum py-2.5 pr-4 text-right text-[13px] text-[var(--ink-4)]">
                {formatMinor(line.tax_minor, quote.currency)}
              </td>
              <td className="tnum py-2.5 text-right text-[13px] font-semibold text-[var(--ink)]">
                {formatMinor(line.subtotal_minor, quote.currency)}
              </td>
            </tr>
          ))}
          {rows.map((row) => (
            <tr key={row.label} className="border-t border-[var(--card-line)]">
              <th scope="row" colSpan={4} className="py-2 pr-4 text-left text-[13px] font-normal text-[var(--ink-3)]">
                {row.label}
              </th>
              <td
                className={cx(
                  "tnum py-2 text-right text-[13px]",
                  row.muted ? "text-[var(--ink-4)]" : "text-[var(--ink-2)]",
                )}
              >
                {formatMinor(row.minor, quote.currency)}
              </td>
            </tr>
          ))}
          <tr className="border-t-2 border-[var(--ink)]">
            <th scope="row" colSpan={4} className="py-3 pr-4 text-left text-[14px] font-bold text-[var(--ink)]">
              Total
            </th>
            <td className="py-3 text-right">
              <Amount money={quote.total} className="text-[18px] font-extrabold text-[var(--ink)]" />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

/* -------------------------------------------------------------------- card */

export function ApprovalCard({
  card,
  busy = null,
  error,
  onApprove,
  onReject,
  autoRead = false,
}: {
  card: ApprovalCardData;
  /** Which control is in flight, so both can be disabled and only one shows a spinner. */
  busy?: "approve" | "reject" | null;
  error?: string | null;
  onApprove: () => void;
  onReject: () => void;
  /** Reached by voice: read the card aloud without waiting for a press. */
  autoRead?: boolean;
}) {
  const names: Record<string, string> = {};
  for (const line of card.quote?.lines ?? []) names[line.sku] = line.name;

  return (
    <div className="flex flex-col gap-5">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <Badge tone="green">Version {card.version}</Badge>
            {card.previous_version !== null ? (
              <span className="text-[12px] text-[var(--ink-4)]">
                replaces version {card.previous_version}
              </span>
            ) : null}
          </div>
          <h1 className="mt-2 text-[20px] font-extrabold text-[var(--ink)]">
            Approve this order
          </h1>
          <p className="mt-1 max-w-[60ch] text-[13px] leading-[1.55] text-[var(--ink-3)]">
            Your approval binds to version {card.version} and to the exact bytes hashed below.
            It is not an approval of &ldquo;this basket&rdquo; in general.
          </p>
        </div>
        <div className="text-right">
          <p className="text-[12px] font-semibold text-[var(--ink-4)]">Amount you are approving</p>
          <Amount money={card.total} className="text-[28px] leading-tight font-extrabold text-[var(--ink)]" />
        </div>
      </header>

      {card.deltas.length > 0 ? (
        <section
          aria-label="What changed since the version you last saw"
          className="rounded-[var(--r-md)] border border-amber-200 bg-amber-50 px-4 py-4"
        >
          <h2 className="text-[14px] font-bold text-[#8a5a00]">
            This version is not the one you saw before
          </h2>
          <p className="mt-1 mb-3 max-w-[70ch] text-[13px] text-[var(--ink-2)]">
            The merchant changed something after the previous version was priced. These are the
            differences the server recorded.
          </p>
          <DeltaTable deltas={card.deltas} currency={card.currency} names={names} />
        </section>
      ) : null}

      {card.quote ? (
        <section aria-label="The priced lines this approval covers">
          <h2 className="mb-2 text-[14px] font-bold text-[var(--ink)]">What is in it</h2>
          <QuoteBreakdown quote={card.quote} />
          {card.quote.free_delivery_applied ? (
            <p className="mt-2 text-[12px] text-[var(--green)]">
              Free delivery was applied to this quote by the merchant.
            </p>
          ) : null}
        </section>
      ) : (
        <section
          aria-label="The priced lines this approval covers"
          className="rounded-[var(--r-md)] border border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-3"
        >
          <h2 className="text-[14px] font-bold text-[var(--ink)]">What is in it</h2>
          <p className="mt-1 max-w-[70ch] text-[13px] text-[var(--ink-3)]">
            This response carried the approved amount and its hash but not the line-by-line
            breakdown. The binding figure is the total above, under the content hash below, and
            this storefront will not reconstruct a breakdown it was not sent.
          </p>
        </section>
      )}

      <section
        aria-label="What this approval is bound to"
        className="rounded-[var(--r-md)] border border-[var(--card-line)] bg-white px-4 py-4"
      >
        <h2 className="mb-3 text-[14px] font-bold text-[var(--ink)]">What it is bound to</h2>
        <dl className="flex flex-col gap-3">
          <TrustedFact term="Version">
            <span className="tnum font-semibold">{card.version}</span> — an immutable document. A
            change to the price or the items does not edit it; it ends it and starts the next one.
          </TrustedFact>
          <TrustedFact term="Content hash">
            <HashChip value={card.content_hash} label="Content hash" /> — the canonical hash of
            those bytes. The kernel compares this at payment time.
          </TrustedFact>
          <TrustedFact term="Policy receipt">
            <HashChip value={card.policy_receipt_hash} label="Policy receipt hash" /> — the merchant
            rules in force when this was priced, recorded so the sale can be explained later.
          </TrustedFact>
          <TrustedFact term="Stock hold">
            {card.reservation ? (
              <span className="flex flex-col gap-1">
                <ReservationCountdown expiresAt={card.reservation.expires_at ?? card.expires_at} />
                <span className="text-[12px] text-[var(--ink-4)]">
                  Reservation{" "}
                  <code className="font-mono text-[11px] break-all">{card.reservation.reservation_id}</code>,
                  state <code className="font-mono text-[11px]">{card.reservation.state}</code>.
                </span>
              </span>
            ) : (
              <ReservationCountdown expiresAt={card.expires_at} />
            )}
          </TrustedFact>
        </dl>
      </section>

      <TrustedSurface
        label="You are approving this. RazorAI cannot."
        caption={
          <>
            RazorAI can put items in your basket and it can ask for this screen. It holds no
            capability to approve or to pay, so this decision is only ever yours. Approving records
            your consent against version {card.version} and hash{" "}
            <code className="font-mono text-[12px]">{card.content_hash.slice(0, 12)}…</code>; it does
            not charge you. Payment is a separate press.
          </>
        }
      >
        {error ? (
          <p role="alert" className="mb-3 text-[13px] font-semibold text-[var(--red)]">
            {error}
          </p>
        ) : null}
        <TrustedActions>
          <Button size="lg" onClick={onApprove} busy={busy === "approve"} disabled={busy !== null}>
            Approve <Amount money={card.total} className="font-extrabold" />
          </Button>
          <Button variant="danger" size="lg" onClick={onReject} busy={busy === "reject"} disabled={busy !== null}>
            Reject this version
          </Button>
        </TrustedActions>
        {/*
          A second way to press the button above, beside it and inside the same trusted
          frame. It is handed the button's own callback and the card on screen, and it
          calls the callback only when what the gateway read aloud matches that card in
          every binding field. The button, its request and its refusal rendering are
          exactly as they were.
        */}
        <VoiceConsent card={card} busy={busy} onApprove={onApprove} autoRead={autoRead} />
      </TrustedSurface>
    </div>
  );
}
