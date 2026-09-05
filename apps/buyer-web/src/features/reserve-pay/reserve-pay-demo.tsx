/**
 * A labelled simulator for the Reserve Pay mandate + approval step -- and nothing more.
 *
 * WHY THIS SCREEN IS ALLOWED TO EXIST
 * -----------------------------------
 * This storefront ships no fixture: an unreachable API renders an error, not invented
 * data, and a silent fake on the money path would be the one screen a reviewer could
 * point at to argue the rest of the evidence is decorative. Reserve Pay is the exception
 * the specification itself carves out. PROJECT_SPECIFICATION.md section 12.3 requires
 * "an explicitly labelled simulator for the unavailable external step" and forbids
 * labelling a simulated provider call as live Razorpay/NPCI execution. This is that
 * simulator, and it is marked as one in the UI, permanently and unmissably.
 *
 * WHAT IT TEACHES
 * ---------------
 * It walks the buyer through the authority a Reserve Pay mandate would grant, using the
 * REAL kernel vocabulary from `transaction_kernel/authority.py` so the demo teaches the
 * actual model rather than a plausible-looking imitation:
 *
 *   - kind: SINGLE_USE | RESERVE (AuthorityKind) -- how much one debit consumes.
 *   - merchant / buyer_ref scope (check_authority: MERCHANT_OUT_OF_SCOPE, BUYER_OUT_OF_SCOPE).
 *   - max_amount, the cumulative capacity a RESERVE blocks (CAPACITY_EXCEEDED).
 *   - a per-action ceiling the buyer chooses (spec 12.1 step 5: "per-action amount").
 *   - currency, compared before any arithmetic (CURRENCY_MISMATCH).
 *   - expiry (AuthorityStatus.EXPIRED, decided by the database clock, never here).
 *   - revocation, carried as a revocation epoch (AuthorityStatus.REVOKED, spec 12.1 step 5).
 *
 * WHAT IT DOES NOT DO
 * -------------------
 * It touches no kernel, creates no payment, and calls no admission route. "Grant" moves
 * a React state variable and says on screen that nothing was recorded. There is no API
 * client imported into this file at all -- the absence is the guarantee, and a test holds
 * it there.
 *
 * Money is integer minor units (paise) throughout, rendered through the shared `Amount`
 * component. The per-transaction and cumulative caps are scenario inputs the buyer picks
 * (spec 12.4: no arbitrary universal caps), not platform limits.
 */
"use client";

import { useId, useState } from "react";

import { Amount, Badge, Button, Card, cx } from "@/components/ui";
import { formatMinor } from "@/lib/money";

/** The two authority kinds the kernel actually has (AuthorityKind, authority.py). */
type Kind = "SINGLE_USE" | "RESERVE";

/**
 * The mandate the buyer is shaping, in kernel terms. Every field here maps to something
 * `admit_debit` / `check_authority` genuinely reads; none is invented.
 */
interface DraftMandate {
  kind: Kind;
  merchant: string;
  /** Cumulative capacity a RESERVE blocks -- kernel `max_amount`, integer paise. */
  cumulativeCapMinor: number;
  /** Per-action ceiling the buyer sets -- spec 12.1 step 5 "per-action amount", paise. */
  perActionCapMinor: number;
  currency: string;
  /** Days until expiry. Rendered only; the real clock is PostgreSQL's, never the browser's. */
  expiryDays: number;
}

/**
 * Scenario defaults. Chosen for a demonstration, not hard-coded as a platform limit
 * (spec 12.4). A buyer changes them below.
 */
const DEFAULT: DraftMandate = {
  kind: "RESERVE",
  merchant: "RazorSharp Quick Commerce",
  cumulativeCapMinor: 500000, // ₹5,000.00
  perActionCapMinor: 150000, //  ₹1,500.00
  currency: "INR",
  expiryDays: 30,
};

const KINDS: ReadonlyArray<{ value: Kind; title: string; blurb: string }> = [
  {
    value: "RESERVE",
    title: "Reserve (reusable pool)",
    blurb:
      "One blocked pool that many debits draw down until it is exhausted, expires, is released or revoked. The kernel allocates each debit against remaining capacity under a row lock.",
  },
  {
    value: "SINGLE_USE",
    title: "Single-use",
    blurb:
      "Consumed by the first admitted debit and then EXHAUSTED. A second debit is refused with SINGLE_USE_ALREADY_CONSUMED.",
  },
];

/** Amounts the buyer can pick for a cap, in paise. Scenario inputs, not caps we impose. */
const CAP_CHOICES_MINOR = [100000, 250000, 500000, 1000000] as const;
const PER_ACTION_CHOICES_MINOR = [50000, 100000, 150000, 300000] as const;
const EXPIRY_CHOICES_DAYS = [7, 30, 90] as const;

type Phase = "shaping" | "granted";

export function ReservePayDemo({ className }: { className?: string }) {
  const [draft, setDraft] = useState<DraftMandate>(DEFAULT);
  const [phase, setPhase] = useState<Phase>("shaping");
  const headingId = useId();

  /**
   * "Grant" the mandate. This is the whole of it: a state transition. No client is
   * called, no row is written, no epoch is allocated. The banner and the granted panel
   * both say so.
   */
  function grant() {
    setPhase("granted");
  }

  function reset() {
    setPhase("shaping");
  }

  return (
    <section aria-labelledby={headingId} className={cx("mx-auto max-w-3xl px-4 py-6", className)}>
      <SimulationBanner />

      <h1 id={headingId} className="mt-6 text-[22px] font-bold text-[var(--ink)]">
        Reserve Pay mandate — simulated
      </h1>
      <p className="mt-1 max-w-prose text-[13px] leading-[1.5] text-[var(--ink-3)]">
        This walks through the authority a buyer would delegate for Reserve Pay, in the
        kernel&rsquo;s own vocabulary. It is a teaching surface: shaping the mandate and
        approving it change a value on this page and nothing else.
      </p>

      {phase === "shaping" ? (
        <MandateForm draft={draft} onChange={setDraft} onGrant={grant} />
      ) : (
        <GrantedPanel draft={draft} onReset={reset} />
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ banner */

/**
 * The permanent, non-dismissable simulation banner. It cites specification 12.3 by number,
 * states Reserve Pay is held in Safe Mode, and quotes the real argument from
 * docs/SUBMISSION.md rather than inventing a rationale. There is no close control: a
 * simulator that can be dismissed is a fixture waiting to happen.
 */
export function SimulationBanner() {
  return (
    <div
      role="note"
      aria-label="Simulation notice"
      className="rounded-[var(--r-md)] border border-[var(--amber)] bg-amber-50 px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="amber">Simulation</Badge>
        <Badge tone="neutral">Specification 12.3</Badge>
        <Badge tone="red">Safe Mode</Badge>
      </div>
      <p className="mt-2 text-[13px] font-semibold text-[var(--ink)]">
        This is a labelled simulator for the unavailable external step, as specification
        12.3 requires. No mandate exists, no authority was granted, and no money can move
        from this screen.
      </p>
      <p className="mt-2 max-w-prose text-[12px] leading-[1.5] text-[var(--ink-3)]">
        Reserve Pay is held in Safe Mode on purpose. As{" "}
        <span className="font-semibold">docs/SUBMISSION.md</span> puts it: &ldquo;Shipping a
        machine that pays without a human present, and calling it safe, is the exact claim
        this project argues you should not make on the strength of an architecture
        diagram.&rdquo; The controls that would make it defensible — the mandate
        cryptography, the revocation path under live load, the reconciliation of an action
        nobody watched — are not proven yet. Nothing here is a live Razorpay or NPCI call.
      </p>
    </div>
  );
}

/* -------------------------------------------------------------------- form */

function MandateForm({
  draft,
  onChange,
  onGrant,
}: {
  draft: DraftMandate;
  onChange: (next: DraftMandate) => void;
  onGrant: () => void;
}) {
  return (
    <div className="mt-5 grid gap-4">
      {/* Kind ------------------------------------------------------------ */}
      <Card className="px-4 py-4">
        <FieldHeading title="Authority kind" hint="AuthorityKind" />
        <div className="mt-3 grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Authority kind">
          {KINDS.map((option) => {
            const selected = draft.kind === option.value;
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => onChange({ ...draft, kind: option.value })}
                className={cx(
                  "rounded-[var(--r-md)] border p-3 text-left transition",
                  selected
                    ? "border-[var(--green-add)] bg-[var(--green-add-bg)]"
                    : "border-[var(--card-line)] bg-white hover:bg-[var(--tint-2)]",
                )}
              >
                <span className="flex items-center gap-2 text-[14px] font-semibold text-[var(--ink)]">
                  {option.title}
                  {selected ? <Badge tone="green">Selected</Badge> : null}
                </span>
                <span className="mt-1 block text-[12px] leading-[1.45] text-[var(--ink-4)]">
                  {option.blurb}
                </span>
              </button>
            );
          })}
        </div>
      </Card>

      {/* Scope ----------------------------------------------------------- */}
      <Card className="px-4 py-4">
        <FieldHeading title="Scope" hint="merchant_id · buyer_ref" />
        <p className="mt-1 text-[12px] leading-[1.45] text-[var(--ink-4)]">
          The kernel refuses a debit outside the mandate&rsquo;s scope
          (MERCHANT_OUT_OF_SCOPE, BUYER_OUT_OF_SCOPE). This mandate would be bound to one
          merchant.
        </p>
        <dl className="mt-3 grid grid-cols-2 gap-y-2 text-[13px]">
          <dt className="text-[var(--ink-4)]">Merchant</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">{draft.merchant}</dd>
          <dt className="text-[var(--ink-4)]">Currency</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">{draft.currency}</dd>
        </dl>
      </Card>

      {/* Cumulative capacity --------------------------------------------- */}
      <Card className="px-4 py-4">
        <FieldHeading title="Cumulative cap" hint="max_amount" />
        <p className="mt-1 text-[12px] leading-[1.45] text-[var(--ink-4)]">
          The total this reserve blocks. The sum of admitted, non-failed debits can never
          exceed it — a debit past it is refused with CAPACITY_EXCEEDED.
        </p>
        <AmountChoices
          label="Cumulative cap"
          choices={CAP_CHOICES_MINOR}
          value={draft.cumulativeCapMinor}
          currency={draft.currency}
          onSelect={(minor) => onChange({ ...draft, cumulativeCapMinor: minor })}
        />
      </Card>

      {/* Per-action ceiling ---------------------------------------------- */}
      <Card className="px-4 py-4">
        <FieldHeading title="Per-transaction cap" hint="spec 12.1 · per-action amount" />
        <p className="mt-1 text-[12px] leading-[1.45] text-[var(--ink-4)]">
          The most any single debit may draw. The kernel verifies the per-action amount at
          admission (specification 12.1, step 5).
        </p>
        <AmountChoices
          label="Per-transaction cap"
          choices={PER_ACTION_CHOICES_MINOR}
          value={draft.perActionCapMinor}
          currency={draft.currency}
          onSelect={(minor) => onChange({ ...draft, perActionCapMinor: minor })}
        />
      </Card>

      {/* Expiry ----------------------------------------------------------- */}
      <Card className="px-4 py-4">
        <FieldHeading title="Expiry" hint="expires_at → AuthorityStatus.EXPIRED" />
        <p className="mt-1 text-[12px] leading-[1.45] text-[var(--ink-4)]">
          After expiry the authority is EXPIRED and admits nothing. The real expiry is
          decided by the database clock at admission, never recomputed in a browser.
        </p>
        <div className="mt-3 flex flex-wrap gap-2" role="radiogroup" aria-label="Expiry">
          {EXPIRY_CHOICES_DAYS.map((days) => {
            const selected = draft.expiryDays === days;
            return (
              <button
                key={days}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => onChange({ ...draft, expiryDays: days })}
                className={cx(
                  "rounded-[var(--r-sm)] border px-3 py-1.5 text-[13px] font-semibold transition",
                  selected
                    ? "border-[var(--green-add)] bg-[var(--green-add-bg)] text-[var(--green-add)]"
                    : "border-[var(--card-line)] bg-white text-[var(--ink)] hover:bg-[var(--tint-2)]",
                )}
              >
                {days} days
              </button>
            );
          })}
        </div>
      </Card>

      {/* Revocation ------------------------------------------------------- */}
      <Card className="px-4 py-4">
        <FieldHeading title="Revocation" hint="revocation_epoch → AuthorityStatus.REVOKED" />
        <p className="mt-1 text-[12px] leading-[1.45] text-[var(--ink-4)]">
          The buyer can revoke future authority at any time from a trusted surface. A raised
          revocation epoch outranks every other bound and blocks all subsequent debits — in
          the kernel it is checked first, before scope, capacity or expiry.
        </p>
      </Card>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-prose text-[12px] leading-[1.45] text-[var(--ink-4)]">
          Approving this mandate below records nothing. It is a simulated grant.
        </p>
        <Button variant="primary" size="lg" onClick={onGrant}>
          Approve mandate (simulated)
        </Button>
      </div>
    </div>
  );
}

function FieldHeading({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3">
      <h2 className="text-[15px] font-bold text-[var(--ink)]">{title}</h2>
      <code className="text-[11px] text-[var(--ink-4)]">{hint}</code>
    </div>
  );
}

function AmountChoices({
  label,
  choices,
  value,
  currency,
  onSelect,
}: {
  label: string;
  choices: readonly number[];
  value: number;
  currency: string;
  onSelect: (minor: number) => void;
}) {
  return (
    <div className="mt-3 flex flex-wrap gap-2" role="radiogroup" aria-label={label}>
      {choices.map((minor) => {
        const selected = value === minor;
        return (
          <button
            key={minor}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onSelect(minor)}
            className={cx(
              "tnum rounded-[var(--r-sm)] border px-3 py-1.5 text-[13px] font-semibold transition",
              selected
                ? "border-[var(--green-add)] bg-[var(--green-add-bg)] text-[var(--green-add)]"
                : "border-[var(--card-line)] bg-white text-[var(--ink)] hover:bg-[var(--tint-2)]",
            )}
          >
            {formatMinor(minor, currency)}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------- granted panel */

function GrantedPanel({ draft, onReset }: { draft: DraftMandate; onReset: () => void }) {
  const kindTitle = KINDS.find((option) => option.value === draft.kind)?.title ?? draft.kind;

  return (
    <div className="mt-5 grid gap-4" aria-live="polite">
      <Card className="border-[var(--amber)] px-4 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="amber">Simulated grant</Badge>
          <Badge tone="red">Nothing recorded</Badge>
        </div>
        <h2 className="mt-2 text-[16px] font-bold text-[var(--ink)]">
          Mandate approved on screen only
        </h2>
        <p className="mt-1 max-w-prose text-[13px] leading-[1.5] text-[var(--ink-3)]">
          Nothing was written. No authority row was created, no revocation epoch was
          allocated, and no admission route was called. This panel reflects a value held in
          this page&rsquo;s memory, and it disappears on reload.
        </p>
      </Card>

      <Card className="px-4 py-4">
        <h3 className="text-[15px] font-bold text-[var(--ink)]">What this mandate would have said</h3>
        <dl className="mt-3 grid grid-cols-2 gap-y-2 text-[13px]">
          <dt className="text-[var(--ink-4)]">Kind</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">{kindTitle}</dd>

          <dt className="text-[var(--ink-4)]">Merchant scope</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">{draft.merchant}</dd>

          <dt className="text-[var(--ink-4)]">Cumulative cap</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">
            <Amount minor={draft.cumulativeCapMinor} currency={draft.currency} />
          </dd>

          <dt className="text-[var(--ink-4)]">Per-transaction cap</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">
            <Amount minor={draft.perActionCapMinor} currency={draft.currency} />
          </dd>

          <dt className="text-[var(--ink-4)]">Currency</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">{draft.currency}</dd>

          <dt className="text-[var(--ink-4)]">Expiry</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">{draft.expiryDays} days</dd>

          <dt className="text-[var(--ink-4)]">Revocation</dt>
          <dd className="text-right font-semibold text-[var(--ink)]">
            Revocable at epoch 0
          </dd>
        </dl>
      </Card>

      <div>
        <Button variant="ghost" size="md" onClick={onReset}>
          Shape another (simulated)
        </Button>
      </div>
    </div>
  );
}
