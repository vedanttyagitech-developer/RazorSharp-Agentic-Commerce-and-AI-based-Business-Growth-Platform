/**
 * The Reserve Pay debit ledger: what autonomous debits against a mandate WOULD look like.
 *
 * A reserve is a block of funds the buyer authorized once on a trusted surface; the agent
 * then debits against it as value is delivered, without the buyer present for each one.
 * The interesting screen is not the happy path but the refusals -- the point of modelling
 * payment authority as a stateful, revocable, consumable resource is that the kernel can
 * confidently say NO, and the rows below are the shapes of that NO.
 *
 * WHY THIS IS A SIMULATOR, AND WHY THAT IS ALLOWED
 * ------------------------------------------------
 * This platform ships no fixtures: an unreachable API renders an error rather than invented
 * data, and inventing data on the money path would be the worst place to do it. But there
 * is no live Reserve Pay debit here to read -- the build account has no Reserve Pay API
 * capability, and the kernel holds this whole path in Safe Mode deliberately. So this is
 * the one honest way the specification itself sanctions: PROJECT_SPECIFICATION.md section
 * 12.3 requires "an explicitly labelled simulator for the unavailable external step" and
 * forbids labelling a simulated provider call as live. Every row here is a rehearsal, not
 * a record. Nothing below reads an API, and nothing below moves money.
 *
 * WHAT IS REAL IN IT
 * ------------------
 * The vocabulary. The states, the refusal reasons and the shape of an authority all come
 * from the transaction kernel that DOES exist and IS tested: `admit_debit` in
 * `packages/transaction-kernel/.../authority.py`, the `AuthorityReason` enum beside it,
 * `Operation.RESERVE_DEBIT` and `VerifiedAuthorityProof` in `contracts.py`. The arithmetic
 * is real too: capacity is integer minor units and every remaining figure is integer
 * subtraction that the kernel's own CHECK constraint would refuse to let go negative.
 */
"use client";

import { Amount, Badge, Card, cx } from "@/components/ui";
import { formatMinor } from "@/lib/money";

/* --------------------------------------------------------------------- vocabulary */

/**
 * The outcome of one simulated debit, in the kernel's own reason vocabulary.
 *
 * `ADMITTED` mirrors `admit_debit` returning `RecoveryCode.OK`: the amount was allocated
 * against the reserve's capacity under the row lock. Each refusal names the exact
 * `AuthorityReason` the kernel would attach -- these are not decorative strings, they are
 * the closed enum keys from `authority.py` that an agent renders into the buyer's language
 * but may never alter.
 */
type DebitOutcome = "ADMITTED" | "REFUSED";

/**
 * A refusal reason. `reason` is the literal `AuthorityReason` member; `recovery` is the
 * `RecoveryCode` that travels with it (the outward-facing code a caller acts on); `glyph`
 * and `word` carry the refusal without relying on colour, per the precedent in
 * `order-detail.tsx`.
 */
interface Refusal {
  /** Literal AuthorityReason enum key from the kernel. */
  reason: string;
  /** The RecoveryCode this reason travels with. */
  recovery: string;
  /** One line, in the buyer's language, on what the kernel refused and why. */
  explain: string;
  glyph: "cross" | "capacity" | "revoked";
  word: string;
}

/** One row of the simulated ledger. */
interface DebitRow {
  id: string;
  /** What the agent proposed to buy -- the delivery/order evidence behind the debit. */
  bought: string;
  /** The debit amount, in integer minor units (paise). Never a float. */
  amountMinor: number;
  /** ISO timestamp of the simulated debit attempt. */
  at: string;
  outcome: DebitOutcome;
  /** Present only on a REFUSED row. */
  refusal?: Refusal;
}

/**
 * The mandate these debits run against. Every figure is integer minor units.
 *
 * `perDebitCapMinor` is the mandate's per-action amount bound (specification 12.1 step 5,
 * "per-action amount") -- a limit the BUYER chose when establishing the reserve, not a
 * platform-invented universal cap (specification 12.4 forbids those). `capacityMinor` is
 * the maximum blocked amount. `epoch` is the revocation epoch: a debit must present the
 * epoch the authority is currently at, or the kernel refuses it as lapsed.
 */
interface Mandate {
  merchant: string;
  currency: string;
  capacityMinor: number;
  perDebitCapMinor: number;
  epoch: number;
}

/* ------------------------------------------------------------------ the scenario */

/**
 * A demonstration reserve. These are SCENARIO INPUTS, not platform limits: the buyer chose
 * ₹5,000 blocked with a ₹1,500 per-debit ceiling (specification 12.4 -- demo values are
 * scenario inputs, never hard-coded product caps).
 */
const MANDATE: Mandate = {
  merchant: "RazorSharp Quick Commerce",
  currency: "INR",
  capacityMinor: 500_000,
  perDebitCapMinor: 150_000,
  epoch: 0,
};

/**
 * The scripted sequence of debits. Ordered as they would arrive over the mandate's life:
 * three admitted debits that draw the reserve down, then the three refusals that are the
 * whole point of the screen.
 *
 * The refusal reasons are the kernel's, verbatim:
 *  - the per-debit ceiling case travels as `AUTHORITY_INSUFFICIENT`, the same RecoveryCode
 *    the kernel returns for every capacity failure; the mandate's per-action amount bound
 *    is what it exceeds.
 *  - `CAPACITY_EXCEEDED` is the literal `AuthorityReason` for `amount > snapshot.remaining`.
 *  - `AUTHORITY_EPOCH_STALE` is the literal reason when the presented epoch is below the
 *    authority's revocation epoch -- the lapse-mid-flight case (see the action-executor test
 *    `test_fs_authority_lapses_midflight.py`, where a withdrawn authority buries the command
 *    with `AUTHORITY_INSUFFICIENT` and never reaches the provider).
 */
const DEBITS: readonly DebitRow[] = [
  {
    id: "d1",
    bought: "Milk, bread, eggs — morning delivery",
    amountMinor: 48_000,
    at: "2026-09-05T06:12:00.000Z",
    outcome: "ADMITTED",
  },
  {
    id: "d2",
    bought: "Detergent + household refill",
    amountMinor: 132_500,
    at: "2026-09-05T09:41:00.000Z",
    outcome: "ADMITTED",
  },
  {
    id: "d3",
    bought: "Fruit & vegetables — evening delivery",
    amountMinor: 90_000,
    at: "2026-09-05T18:03:00.000Z",
    outcome: "ADMITTED",
  },
  {
    id: "d4",
    bought: "Bulk grocery restock",
    amountMinor: 175_000,
    at: "2026-09-06T07:20:00.000Z",
    outcome: "REFUSED",
    refusal: {
      reason: "AUTHORITY_INSUFFICIENT",
      recovery: "AUTHORITY_INSUFFICIENT",
      explain:
        "This one debit is larger than the per-action amount the buyer set when they established the reserve. The kernel checks the per-action bound before it touches remaining capacity, so nothing was allocated.",
      glyph: "cross",
      word: "Over per-debit limit",
    },
  },
  {
    id: "d5",
    bought: "Weekend party supplies",
    amountMinor: 240_000,
    at: "2026-09-06T11:15:00.000Z",
    outcome: "REFUSED",
    refusal: {
      reason: "CAPACITY_EXCEEDED",
      recovery: "AUTHORITY_INSUFFICIENT",
      explain:
        "The reserve does not have this much capacity left. The kernel refuses when the debit exceeds remaining cumulative capacity — spending against an unknown or absent balance is how a double debit happens.",
      glyph: "capacity",
      word: "Exceeds remaining capacity",
    },
  },
  {
    id: "d6",
    bought: "Cleaning service booking",
    amountMinor: 30_000,
    at: "2026-09-06T14:48:00.000Z",
    outcome: "REFUSED",
    refusal: {
      reason: "AUTHORITY_EPOCH_STALE",
      recovery: "AUTHORITY_REVOKED",
      explain:
        "The buyer revoked or changed the reserve, which raised its revocation epoch. This debit still carried the old epoch, so the kernel treats the authority as lapsed and refuses — revocation outranks every other bound.",
      glyph: "revoked",
      word: "Authority lapsed (epoch moved)",
    },
  },
];

/* -------------------------------------------------------------------- computation */

/**
 * Walk the scripted debits and compute the remaining capacity after each one.
 *
 * Integer subtraction on minor units only. An ADMITTED debit draws the reserve down; a
 * REFUSED debit allocates nothing, so capacity is unchanged. The running figure can never
 * go negative, because the kernel only admits a debit when `amount <= remaining`, and this
 * mirror allocates under the same rule — asserted in the colocated test.
 */
export function walkLedger(
  rows: readonly DebitRow[],
  capacityMinor: number,
): Array<{ row: DebitRow; remainingMinor: number; admitted: boolean }> {
  let remaining = capacityMinor;
  return rows.map((row) => {
    const admitted = row.outcome === "ADMITTED" && row.amountMinor <= remaining;
    if (admitted) {
      remaining -= row.amountMinor;
    }
    // Belt beyond the check: never render a reserve that claims less than nothing left.
    if (remaining < 0) remaining = 0;
    return { row, remainingMinor: remaining, admitted };
  });
}

/* ------------------------------------------------------------------------ glyphs */

/**
 * A refusal glyph, drawn without colour so the reason survives a monochrome print or a
 * colour-blind reader — the precedent is `RefundGlyph` in `order-detail.tsx`.
 */
function RefusalGlyph({ kind }: { kind: Refusal["glyph"] }) {
  const common = {
    viewBox: "0 0 16 16",
    width: 14,
    height: 14,
    fill: "none",
    "aria-hidden": true,
  } as const;
  if (kind === "capacity") {
    // A drained gauge.
    return (
      <svg {...common}>
        <rect x="2.5" y="5" width="11" height="6" rx="1.4" stroke="currentColor" strokeWidth="1.3" />
        <path d="M4.4 6.6h2.2v2.8H4.4z" fill="currentColor" />
      </svg>
    );
  }
  if (kind === "revoked") {
    // A circle with a bar: authority withdrawn.
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="5.8" stroke="currentColor" strokeWidth="1.3" />
        <path d="M4.2 4.2 11.8 11.8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    );
  }
  // cross: over the per-debit limit.
  return (
    <svg {...common}>
      <circle cx="8" cy="8" r="5.8" stroke="currentColor" strokeWidth="1.3" />
      <path d="M5.9 5.9 10.1 10.1M10.1 5.9 5.9 10.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

/* ------------------------------------------------------------------------ banner */

/**
 * The permanent simulation banner. Specification 12.3 requires an explicitly labelled
 * simulator for the unavailable external step, and forbids labelling a simulated provider
 * call as live. This banner is that label, and it never conditionally hides.
 */
export function SimulationBanner() {
  return (
    <div
      role="note"
      aria-label="Reserve Pay debit simulation notice"
      className="rounded-[var(--r-md)] border-[0.5px] border-[var(--amber)] bg-amber-50/50 px-4 py-3"
    >
      <p className="flex items-center gap-2 text-[13px] font-bold text-[var(--ink)]">
        <span
          aria-hidden="true"
          className="inline-flex h-4 w-4 items-center justify-center rounded-full border-[1.5px] border-[var(--amber)] text-[var(--amber)]"
        >
          <svg viewBox="0 0 16 16" width="10" height="10" fill="none" aria-hidden="true">
            <path d="M8 4.6v4.2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            <circle cx="8" cy="11.4" r="0.9" fill="currentColor" />
          </svg>
        </span>
        Simulation — specification 12.3
      </p>
      <p className="mt-1.5 max-w-prose text-[12px] leading-[1.5] text-[var(--ink-3)]">
        No debit occurred and no reserve authority exists. Every row below is a rehearsal of
        what an autonomous Reserve Pay debit <span className="font-semibold">would</span> look
        like against a mandate — the states and refusal reasons are the transaction kernel&rsquo;s
        real vocabulary, but nothing here reads a provider or moves money. In the live kernel this
        whole path is held in Safe Mode: <code className="text-[var(--ink-2)]">GuardedOperation.AUTONOMOUS_COMPLETION</code>{" "}
        is in the Safe-Mode blocked set, so machine-initiated completion is stopped at admission.
        This is a labelled simulator for the unavailable external step, never a live
        Razorpay/NPCI execution.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------------ ledger */

export function DebitLedger({ className }: { className?: string }) {
  const walked = walkLedger(DEBITS, MANDATE.capacityMinor);
  const admittedCount = walked.filter((entry) => entry.admitted).length;
  const remainingMinor = walked.length > 0 ? walked[walked.length - 1].remainingMinor : MANDATE.capacityMinor;

  return (
    <section aria-label="Reserve Pay debit ledger (simulated)" className={cx("grid gap-3", className)}>
      <SimulationBanner />

      {/* The mandate this ledger runs against. */}
      <Card className="px-4 py-4">
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <h2 className="text-[15px] font-bold text-[var(--ink)]">Reserve at {MANDATE.merchant}</h2>
            <p className="mt-1 text-[12px] leading-[1.5] text-[var(--ink-4)]">
              A single block of funds the buyer authorized once. The agent debits against it as value
              is delivered, until it is exhausted, expires, or the buyer revokes it.
            </p>
          </div>
          <div className="text-right">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-[var(--ink-4)]">
              Remaining capacity
            </p>
            <Amount
              minor={remainingMinor}
              currency={MANDATE.currency}
              className="text-[24px] font-bold text-[var(--ink)]"
            />
            <p className="tnum mt-0.5 text-[11px] text-[var(--ink-4)]">
              of {formatMinor(MANDATE.capacityMinor, MANDATE.currency)} blocked
            </p>
          </div>
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 border-t border-[var(--card-line)] pt-3 sm:grid-cols-3">
          <div>
            <dt className="text-[11px] font-semibold text-[var(--ink-4)]">Per-debit limit</dt>
            <dd className="tnum mt-0.5 text-[13px] text-[var(--ink)]">
              {formatMinor(MANDATE.perDebitCapMinor, MANDATE.currency)}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] font-semibold text-[var(--ink-4)]">Revocation epoch</dt>
            <dd className="tnum mt-0.5 text-[13px] text-[var(--ink)]">{MANDATE.epoch}</dd>
          </div>
          <div>
            <dt className="text-[11px] font-semibold text-[var(--ink-4)]">Debits admitted</dt>
            <dd className="tnum mt-0.5 text-[13px] text-[var(--ink)]">
              {admittedCount} of {walked.length}
            </dd>
          </div>
        </dl>
      </Card>

      {/* The ledger itself. A list, so it reads in order without the visuals. */}
      <Card className="overflow-hidden">
        <h2 className="border-b border-[var(--card-line)] px-4 py-3 text-[14px] font-bold text-[var(--ink)]">
          Simulated debits · Operation RESERVE_DEBIT
        </h2>
        <ol>
          {walked.map(({ row, remainingMinor: remaining, admitted }, index) => (
            <li
              key={row.id}
              className={cx(
                "px-4 py-3",
                index < walked.length - 1 && "border-b border-[var(--card-line)]",
              )}
            >
              <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-semibold text-[var(--ink)]">{row.bought}</p>
                  <p className="tnum mt-0.5 text-[11px] text-[var(--ink-4)]" title={row.at}>
                    {row.at}
                  </p>
                </div>
                <div className="text-right">
                  <Amount
                    minor={row.amountMinor}
                    currency={MANDATE.currency}
                    className={cx(
                      "text-[15px] font-bold",
                      admitted ? "text-[var(--ink)]" : "text-[var(--ink-4)] line-through",
                    )}
                  />
                  <div className="mt-1 flex justify-end">
                    {admitted ? (
                      <Badge tone="green">Admitted</Badge>
                    ) : (
                      <Badge tone="red">Refused</Badge>
                    )}
                  </div>
                </div>
              </div>

              {admitted ? (
                <p className="tnum mt-2 text-[11px] text-[var(--ink-3)]">
                  Allocated against the reserve · {formatMinor(remaining, MANDATE.currency)} capacity
                  remaining
                </p>
              ) : row.refusal ? (
                <div className="mt-2 rounded-[var(--r-md)] border-[0.5px] border-[var(--red)] bg-red-50/40 px-3 py-2">
                  <p className="flex items-center gap-1.5 text-[12px] font-semibold text-[var(--red)]">
                    <RefusalGlyph kind={row.refusal.glyph} />
                    {row.refusal.word}
                  </p>
                  <p className="mt-1 max-w-prose text-[12px] leading-[1.5] text-[var(--ink-3)]">
                    {row.refusal.explain}
                  </p>
                  <p className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-[var(--ink-4)]">
                    <span>
                      Reason: <code className="text-[var(--ink-2)]">{row.refusal.reason}</code>
                    </span>
                    <span>
                      Recovery: <code className="text-[var(--ink-2)]">{row.refusal.recovery}</code>
                    </span>
                  </p>
                  <p className="tnum mt-1 text-[11px] text-[var(--ink-4)]">
                    Nothing allocated · {formatMinor(remaining, MANDATE.currency)} capacity unchanged
                  </p>
                </div>
              ) : null}
            </li>
          ))}
        </ol>
      </Card>
    </section>
  );
}
