/**
 * How the platform learned that money moved, rendered so a reviewer can check the claim.
 *
 * The claim is narrow and worth stating exactly: a capture is applied only from evidence
 * the platform verified against the provider itself, which is either a webhook whose
 * signature it checked or a fetch it made to the provider's API. The browser's return
 * from the payment page is recorded as a buyer event and applies nothing (ADR 0003 D8),
 * so `kind` on a capture-evidence record is never BROWSER_CALLBACK. An order that exists
 * is therefore an order one of those two verified sources put there.
 *
 * This module is the leaf of the three order screens -- it imports from neither sibling --
 * which is why the timestamp formatter every one of them needs lives here rather than in
 * a fourth file none of them owns.
 */
"use client";

import type { ReactNode } from "react";

import { Badge, Card } from "@/components/ui";
import type { Attempt } from "@/lib/api/types";

/**
 * Derived from the attempt rather than redeclared: `types.ts` owns every wire shape, and
 * a second declaration of this one is a second thing to keep in step with the server.
 */
export type CaptureEvidence = NonNullable<Attempt["capture_evidence"]>;

/**
 * An instant the server stamped, in the shop's own time. Falls back to the raw string.
 *
 * `Asia/Kolkata` and not the reader's own zone. This is an Indian shop -- rupees, en-IN,
 * a thirty-minute delivery promise -- and a capture stamped at half past two in the
 * afternoon should read as half past two to everyone discussing it, including a reviewer
 * opening the page from somewhere else. It is also what keeps a server-rendered
 * timestamp identical to the one the browser draws over it: a fixed zone agrees with
 * itself, and "local" agrees with nothing.
 */
export function formatTimestamp(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    dateStyle: "medium",
    timeStyle: "medium",
  });
}

/**
 * The two kinds that can appear, and what each one actually asserts.
 *
 * A kind outside this table renders as itself. The server owns the vocabulary and an
 * unrecognised value is information, not a reason to draw nothing.
 */
const KINDS: Readonly<Record<string, { label: string; short: string; meaning: string }>> = {
  WEBHOOK: {
    label: "Signed provider webhook",
    short: "Webhook",
    meaning:
      "Razorpay sent this event and the platform verified its signature before applying it.",
  },
  PROVIDER_FETCH: {
    label: "Direct fetch from the provider",
    short: "Provider fetch",
    meaning:
      "The platform asked Razorpay's API about this payment and read the answer itself.",
  },
};

function WebhookGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none">
      <path
        d="M8 1.5 13.5 4v4.2c0 3-2.3 5.4-5.5 6.3C4.8 13.6 2.5 11.2 2.5 8.2V4L8 1.5Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path d="M5.6 8.1 7.3 9.8l3.2-3.4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function FetchGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none">
      <path d="M8 2v8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M4.8 6.9 8 10.1l3.2-3.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2.6 12.6h10.8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function AbsentGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none">
      <circle cx="8" cy="8" r="5.6" stroke="currentColor" strokeWidth="1.3" />
      <path d="M4.4 11.6 11.6 4.4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  );
}

function glyphFor(kind: string): ReactNode {
  if (kind === "WEBHOOK") return <WebhookGlyph />;
  if (kind === "PROVIDER_FETCH") return <FetchGlyph />;
  return <AbsentGlyph />;
}

/**
 * The compact form for a list row: which of the two verified sources confirmed this sale.
 *
 * Carries a glyph as well as a colour so the source is legible without relying on hue.
 */
export function CaptureEvidenceTag({ evidence }: { evidence: CaptureEvidence | null }) {
  if (!evidence) {
    return (
      <Badge tone="neutral">
        <AbsentGlyph />
        No capture evidence
      </Badge>
    );
  }
  const known = KINDS[evidence.kind];
  return (
    <span title={`${evidence.kind} · ${evidence.reference}`}>
      <Badge tone={known ? "blue" : "amber"}>
        {glyphFor(evidence.kind)}
        {known ? known.short : evidence.kind}
      </Badge>
    </span>
  );
}

/**
 * The full record, with the rule it demonstrates spelled out beneath it.
 *
 * The rule is written as something checkable rather than as reassurance: it names the two
 * permitted kinds and the one value that cannot appear, which is a claim a payments
 * reviewer can test by reading any order on this platform.
 */
export function CaptureEvidencePanel({ evidence }: { evidence: CaptureEvidence | null }) {
  const known = evidence ? KINDS[evidence.kind] : undefined;

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-2 border-b-[0.5px] border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-3">
        <span className="text-[var(--ink-3)]">{glyphFor(evidence?.kind ?? "")}</span>
        <h3 className="text-[14px] font-bold text-[var(--ink)]">Capture evidence</h3>
      </div>

      {evidence ? (
        <dl className="grid grid-cols-1 gap-x-6 gap-y-3 px-4 py-4 sm:grid-cols-[132px_minmax(0,1fr)]">
          <dt className="text-[12px] font-semibold text-[var(--ink-4)]">Kind</dt>
          <dd className="text-[13px] text-[var(--ink)]">
            <span className="font-semibold">{known ? known.label : evidence.kind}</span>
            <span className="ml-2 rounded-[var(--r-sm)] bg-[var(--tint-1)] px-1.5 py-0.5 font-mono text-[12px] text-[var(--ink-3)]">
              {evidence.kind}
            </span>
            <p className="mt-1 text-[13px] text-[var(--ink-3)]">
              {known
                ? known.meaning
                : "This kind is not one the storefront recognises. It is shown exactly as the server sent it."}
            </p>
          </dd>

          <dt className="text-[12px] font-semibold text-[var(--ink-4)]">Reference</dt>
          <dd className="font-mono text-[13px] break-all text-[var(--ink)]">{evidence.reference}</dd>

          <dt className="text-[12px] font-semibold text-[var(--ink-4)]">Verified at</dt>
          <dd className="text-[13px] text-[var(--ink)]" title={evidence.verified_at}>
            {formatTimestamp(evidence.verified_at)}
          </dd>
        </dl>
      ) : (
        <p className="px-4 py-4 text-[13px] text-[var(--ink-3)]">
          No capture evidence is recorded on this payment attempt yet. Until one of the two
          verified sources below arrives, the platform treats the money as unconfirmed and
          confirms no sale from it.
        </p>
      )}

      <p className="border-t-[0.5px] border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-3 text-[13px] leading-relaxed text-[var(--ink-3)]">
        A capture is applied only from evidence the platform verified with the provider:{" "}
        <span className="font-mono text-[12px] text-[var(--ink-2)]">WEBHOOK</span>, a Razorpay
        event whose signature was checked, or{" "}
        <span className="font-mono text-[12px] text-[var(--ink-2)]">PROVIDER_FETCH</span>, a
        server-side read of the payment from Razorpay&apos;s API. The browser&apos;s return from
        the payment page is recorded as a buyer event and moves nothing, so this{" "}
        <span className="font-mono text-[12px] text-[var(--ink-2)]">kind</span> is never{" "}
        <span className="font-mono text-[12px] text-[var(--ink-2)]">BROWSER_CALLBACK</span>. That
        is why a confirmed order can be trusted even when the buyer closed the tab.
      </p>
    </Card>
  );
}

/** The one shared class for an identifier a reader compares character by character. */
export const MONO = "font-mono text-[12px] tracking-tight text-[var(--ink-2)] break-all";

/**
 * The titled card every section of the order screens is drawn in.
 *
 * It lives here for the reason the timestamp formatter does: this module is the leaf the
 * order screens share, and both the detail screen and the action panel need this box.
 * Two copies of it would be two things to keep in step, and the first time they drifted
 * the buyer's controls would stop looking like the rest of the page they sit on.
 */
export function SectionCard({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  return (
    <Card className="overflow-hidden">
      <div className="border-b-[0.5px] border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-3">
        <h2 className="text-[14px] font-bold text-[var(--ink)]">{title}</h2>
        {subtitle ? <p className="mt-0.5 text-[12px] text-[var(--ink-4)]">{subtitle}</p> : null}
      </div>
      <div className="px-4 py-4">{children}</div>
    </Card>
  );
}
