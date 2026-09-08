/**
 * The refusal. This is the screen the whole submission is about.
 *
 * A buyer approved one thing; by the time they pressed Pay, something had moved. The
 * kernel compared the approval against the current state and would not spend it. That is
 * not an error — it is the single thing an agentic commerce stack has to get right, and it
 * is the reason a machine may propose a purchase without being able to complete one that
 * drifted.
 *
 * So the screen holds still. It says what was refused, what moved, by how much, and what
 * the buyer can do next, in that order, and it does not apologise for a system behaving
 * exactly as designed. `aria-live="assertive"` because this interrupts: a person who has
 * just pressed Pay must be told immediately where they stand.
 *
 * **Nothing here is said about every refusal that is only true of one of them.** Nine
 * codes arrive at this component and they mean different things. Most of them create
 * nothing at all, and for those the card says so in the strongest words it has: you were
 * not charged, no attempt exists. `DUPLICATE_OPERATION` is the opposite — it is the answer
 * a second submission gets when an attempt for this checkout is *already live*, possibly
 * already authorised, possibly with a capture webhook in flight — so on that one the card
 * names the attempt and refuses to make any claim about money at all. Only the re-approval
 * path carries a successor version, so only the re-approval path draws a version trail or
 * offers to approve one. A screen whose job is to make a refusal legible cannot describe
 * four of nine refusals wrongly.
 *
 * Where the card cannot tell the buyer whether money moved, it says that in those words
 * and then shows the evidence the platform actually holds — the attempt's own state, its
 * provider order and payment ids, and whether any capture evidence has been applied to it
 * — rather than pointing vaguely at something further down the page. Every one of those
 * comes from the re-read checkout; a field the server did not send is named as absent.
 *
 * The buttons say what pressing them does. Every one of them re-reads the checkout and
 * nothing more, so none of them is labelled as an approval, a cancellation or a journey to
 * somewhere else: consent to a superseding version is given on that version's own card.
 *
 * Every figure comes from the decision the kernel returned. The one computed number on
 * the screen is a difference between two integers the server sent.
 */
"use client";

import type { ReactNode } from "react";

import Link from "next/link";

import { Badge, Button, cx } from "@/components/ui";
import { deltaMinor, formatDelta, formatMinor } from "@/lib/money";
import type { Checkout, Delta, SubmitResult } from "@/lib/api/types";

import { DeltaTable } from "./delta-table";
import { HashChip } from "./approval-card";
import { TrustedActions, TrustedSurface } from "./trusted-surface";

/* ------------------------------------------------------- the kernel's words */

/**
 * The kernel's reason keys as English sentences.
 *
 * Keys, not prose, is the right thing for the kernel to return: a sentence in an API
 * response is a sentence somebody eventually translates badly or greps for. Turning the
 * key into a sentence is this layer's job, and a key with no entry here is shown as
 * itself rather than replaced with a comforting guess.
 */
const REASONS: Readonly<Record<string, string>> = {
  merchant_state_changed_since_approval:
    "The merchant's price or availability changed between the moment you approved and the moment you pressed Pay.",
  approved_hash_does_not_match_stored:
    "The bytes you approved are not the bytes now stored for this version, so the approval does not describe this order any more.",
  a_newer_version_exists:
    "A newer version of this order already exists, so the version you approved is no longer the current one.",
  version_already_invalidated:
    "The version you approved had already been retired before payment began.",
  checkout_version_not_found:
    "The version you approved is not on record, so there is nothing for the approval to bind to.",
  reservation_not_valid:
    "The stock held for this order is no longer held, so the items you approved cannot be guaranteed at that price.",
  safe_mode_blocks_operation:
    "Safe Mode is switched on. The platform is deliberately refusing to move money right now, and it refuses rather than queues.",
  principal_lacks_submit_capability:
    "The session that asked for this payment does not hold the capability to submit one. Consent is not delegable to the thing that proposed the purchase.",
  attempt_in_flight:
    "A payment attempt for this checkout is already in flight. The kernel allows exactly one, so a second was refused rather than risking two charges.",
  attempt_already_exists_for_checkout:
    "A payment attempt for this checkout already exists. The kernel allows exactly one, so this second submission was told about the first rather than given an attempt of its own.",
  // States the fact and stops there, because this key is the one reason both the checkout
  // screen and the order screen can receive, and only the fact is common to them. It used
  // to end "so a second submission was refused", which was written for the submit path and
  // read as a lie on the cancel path: a buyer who pressed "Cancel this order" was told a
  // submission had been refused, an event they had not caused. It was redundant even here,
  // because the card's own `underWay` branch below already says a second submission was
  // refused. Each screen names what it refused; this sentence names why.
  payment_surface_open:
    "The payment surface for this checkout is already open, and the platform will not act on top of one that may already be taking a payment.",
  authority_supplied_without_epoch:
    "A delegated authority was presented without the epoch that says which grant of it this is, so it could not be checked against a revocation.",
  cancelled: "This checkout was cancelled, so there is nothing left to pay.",
};

/** The kernel's reason key as a sentence, or null when this app has none for it. */
export function reasonSentence(key: string | null | undefined): string | null {
  if (!key) return null;
  const exact = REASONS[key];
  if (exact) return exact;
  if (key.startsWith("policy_binding_")) {
    return "The merchant policy recorded against this version could not be bound at payment time, so the sale could not be explained under the rules it was priced under.";
  }
  if (key.startsWith("authority_")) {
    return "The delegated authority behind this payment was not accepted: it was revoked, expired, or did not stretch to this amount.";
  }
  return null;
}

/** The recovery code, said as what the buyer can do about it. */
const CODES: Readonly<Record<string, string>> = {
  REAPPROVAL_REQUIRED: "Approve the new version to continue",
  SOLD_OUT: "Everything in this order sold out",
  STALE_CHECKOUT: "This version is out of date",
  RESERVATION_EXPIRED: "The hold on the stock ran out",
  AUTHORITY_REVOKED: "The authority behind this payment was revoked",
  AUTHORITY_INSUFFICIENT: "The authority behind this payment did not cover it",
  CONCURRENT_OPERATION: "Another attempt got there first",
  DUPLICATE_OPERATION: "This request had already been handled",
  SAFE_MODE_ACTIVE: "Safe Mode is on",
  HUMAN_REVIEW_REQUIRED: "A person has to look at this",
};

/** The recovery code as a short phrase, or null when this app has none for it. */
export function codePhrase(code: string | null | undefined): string | null {
  return code ? (CODES[code] ?? null) : null;
}

/**
 * The headline, as a function of the code rather than a constant.
 *
 * "Your approval no longer matches what the merchant is selling" is true of exactly two of
 * these and was printed over all nine, including the Safe Mode refusal, where the approval
 * is untouched and it is the platform that has stopped.
 */
const HEADLINES: Readonly<Record<string, string>> = {
  REAPPROVAL_REQUIRED: "Your approval no longer matches what the merchant is selling.",
  SOLD_OUT: "The merchant can no longer supply anything in this order.",
  STALE_CHECKOUT: "Your approval no longer matches what the merchant is selling.",
  RESERVATION_EXPIRED: "The stock this order was holding is no longer held.",
  SAFE_MODE_ACTIVE: "The platform is refusing to move money right now.",
  HUMAN_REVIEW_REQUIRED: "A person has to look at this before it can go through.",
  CONCURRENT_OPERATION: "This payment was already being handled.",
  DUPLICATE_OPERATION: "This payment was already being handled.",
  AUTHORITY_REVOKED: "The authority behind this payment was not accepted.",
  AUTHORITY_INSUFFICIENT: "The authority behind this payment was not accepted.",
};

function headline(code: string): string {
  return HEADLINES[code] ?? "The transaction kernel refused this submission.";
}

/**
 * Codes on which the kernel created nothing: no grant, no attempt, no provider order.
 *
 * Membership of this set is what licenses the words "you were not charged", so it is a
 * list of codes rather than a default. A code this app has never seen is not assumed to be
 * harmless, because the assumption that costs a buyer their trust is the confident one.
 */
const CREATED_NOTHING: ReadonlySet<string> = new Set([
  "REAPPROVAL_REQUIRED",
  "SOLD_OUT",
  "STALE_CHECKOUT",
  "RESERVATION_EXPIRED",
  "SAFE_MODE_ACTIVE",
  "HUMAN_REVIEW_REQUIRED",
  "POLICY_EXCEPTION",
]);

/** Codes that mean an attempt for this checkout exists and this submission is not it. */
const ALREADY_UNDER_WAY: ReadonlySet<string> = new Set(["DUPLICATE_OPERATION", "CONCURRENT_OPERATION"]);

function createdNothing(code: string): boolean {
  // An authority refusal happens before admission, so nothing downstream of it was made.
  return CREATED_NOTHING.has(code) || code.startsWith("AUTHORITY_");
}

/* ------------------------------------------------------------------ totals */

/** The two totals, taken from the decision if it named them and from the versions if not. */
function totalsFrom(
  deltas: readonly Delta[],
  checkout: Checkout,
  approvedVersion: number,
  nextVersion: number | null,
): { approved: number; current: number } | null {
  const totalDelta = deltas.find(
    (delta) =>
      delta.field_path === "total" &&
      typeof delta.approved === "number" &&
      typeof delta.current === "number",
  );
  if (totalDelta) {
    return { approved: totalDelta.approved as number, current: totalDelta.current as number };
  }
  // With no successor version there is no second total to put beside the first. The read
  // model's current version is not one: on a concurrency refusal it is the same version,
  // and drawing "₹721.95 → ₹721.95" would invent a change out of a comparison with itself.
  if (nextVersion === null) return null;
  const before = checkout.versions.find((version) => version.version === approvedVersion);
  const after =
    checkout.versions.find((version) => version.version === nextVersion) ??
    checkout.versions.find((version) => version.version === checkout.current_version);
  if (!before || !after || before.version === after.version) return null;
  return { approved: before.amount_minor, current: after.amount_minor };
}

function TotalFigure({
  caption,
  minor,
  currency,
  struck = false,
}: {
  caption: string;
  minor: number;
  currency: string;
  struck?: boolean;
}) {
  return (
    <div>
      <p className="text-[12px] font-semibold text-[var(--ink-4)]">{caption}</p>
      <p
        className={cx(
          "tnum mt-0.5 text-[28px] leading-tight font-extrabold",
          struck
            ? "text-[var(--ink-5)] line-through decoration-[var(--red)] decoration-[3px]"
            : "text-[var(--ink)]",
        )}
      >
        {formatMinor(minor, currency)}
      </p>
    </div>
  );
}

function Arrow() {
  return (
    <svg viewBox="0 0 24 12" width="28" height="14" aria-hidden="true" className="text-[var(--ink-6)]">
      <path
        d="M0 6h20M15 1l5 5-5 5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/* ------------------------------------------------- the attempt, as evidence */

/** One labelled fact about the attempt, or the plain statement that it was not sent. */
function EvidenceRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-3">
      <dt className="shrink-0 text-[12px] font-semibold text-[var(--ink-4)] sm:w-44">{term}</dt>
      <dd className="text-[13px] leading-[1.5] text-[var(--ink-2)]">{children}</dd>
    </div>
  );
}

/**
 * What the platform has recorded about the payment attempt that already exists.
 *
 * This is the section that lets the card say "this screen cannot tell you whether money
 * moved" without leaving the buyer nowhere to go. Saying it and then pointing at nothing
 * is only half an honest sentence; the other half is showing them the evidence the
 * platform does hold and naming what is still missing from it.
 *
 * Everything here is the re-read checkout's own `attempt` object printed as it arrived.
 * `capture_evidence` is the field that decides whether money is known to have moved, and
 * a null one is reported as a null one: the platform has not applied a capture, which is
 * not the same claim as the payment having failed. The decision's attempt id is compared
 * against the checkout's rather than merged with it, because two different ids would mean
 * the read has moved on from the refusal and the buyer should be told that, not shown one
 * id standing in for the other.
 */
function AttemptEvidence({
  checkout,
  decidedAttemptId,
}: {
  checkout: Checkout;
  /** The attempt the refusal itself named, which may be null on some codes. */
  decidedAttemptId: string | null;
}) {
  const attempt = checkout.attempt;
  const evidence = attempt?.capture_evidence ?? null;
  const mismatched =
    attempt !== null && decidedAttemptId !== null && attempt.attempt_id !== decidedAttemptId;

  return (
    <section
      aria-label="What the platform has recorded about the payment attempt"
      className="rounded-[var(--r-md)] border border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-4"
    >
      <h2 className="text-[14px] font-bold text-[var(--ink)]">
        What the platform has recorded about that attempt
      </h2>

      {attempt === null ? (
        <div className="mt-2 flex flex-col gap-3">
          <p className="max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-2)]">
            The refusal named a payment attempt but the checkout read back after it carried no
            attempt object, so this screen has nothing to show you about its state and cannot tell
            you whether money moved. Reading the checkout again is the way to ask.
          </p>
          {decidedAttemptId ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12px] font-semibold text-[var(--ink-4)]">
                The attempt the refusal named
              </span>
              <HashChip value={decidedAttemptId} label="Payment attempt id" />
            </div>
          ) : null}
        </div>
      ) : (
        <dl className="mt-3 flex flex-col gap-2">
          <EvidenceRow term="Attempt">
            <HashChip value={attempt.attempt_id} label="Payment attempt id" />
          </EvidenceRow>
          {mismatched && decidedAttemptId ? (
            <EvidenceRow term="The refusal named">
              <span className="flex flex-wrap items-center gap-2">
                <HashChip value={decidedAttemptId} label="Attempt id named by the refusal" />
                <span className="text-[var(--ink-3)]">
                  a different attempt from the one on the checkout now
                </span>
              </span>
            </EvidenceRow>
          ) : null}
          <EvidenceRow term="Against version">
            <span className="tnum font-semibold">{attempt.version}</span>
          </EvidenceRow>
          <EvidenceRow term="Attempt state">
            <code className="rounded-[var(--r-sm)] bg-white px-1.5 py-0.5 font-mono text-[11px] text-[var(--ink-2)]">
              {attempt.state}
            </code>
          </EvidenceRow>
          <EvidenceRow term="Razorpay order">
            {attempt.razorpay_order_id ? (
              <code className="font-mono text-[12px] break-all">{attempt.razorpay_order_id}</code>
            ) : (
              <span className="text-[var(--ink-3)]">none recorded</span>
            )}
          </EvidenceRow>
          <EvidenceRow term="Razorpay payment">
            {attempt.razorpay_payment_id ? (
              <code className="font-mono text-[12px] break-all">{attempt.razorpay_payment_id}</code>
            ) : (
              <span className="text-[var(--ink-3)]">none recorded</span>
            )}
          </EvidenceRow>
          <EvidenceRow term="Capture evidence">
            {evidence ? (
              <span>
                {evidence.kind}, reference{" "}
                <code className="font-mono text-[12px] break-all">{evidence.reference}</code>,
                verified{" "}
                <code className="font-mono text-[12px] break-all">{evidence.verified_at}</code>
              </span>
            ) : (
              <span className="text-[var(--ink-3)]">
                none recorded — no capture has been applied to this attempt from Razorpay&rsquo;s
                evidence, which is not the same as the payment having failed
              </span>
            )}
          </EvidenceRow>
          <EvidenceRow term="Times reconciled">
            <span className="tnum">{attempt.reconciliation_attempts}</span>
          </EvidenceRow>
        </dl>
      )}

      <p className="mt-3 max-w-[70ch] text-[12px] leading-[1.55] text-[var(--ink-4)]">
        A payment is marked captured only from Razorpay&rsquo;s own signed webhook or from the
        platform fetching it directly, never from this browser (ADR 0003 D8). Until capture
        evidence appears above, the platform has not recorded one.{" "}
        {checkout.order_id ? (
          <>
            An order has been written for this checkout:{" "}
            <Link
              href={`/orders/${encodeURIComponent(checkout.order_id)}`}
              className="font-semibold text-[var(--ink)] underline underline-offset-2"
            >
              {checkout.order_reference ?? "see it and its evidence"}
            </Link>
            .
          </>
        ) : (
          <>
            No order has been written for this checkout yet; when one is, it appears in{" "}
            <Link
              href="/orders"
              className="font-semibold text-[var(--ink)] underline underline-offset-2"
            >
              your orders
            </Link>{" "}
            with the evidence it was written from.
          </>
        )}
      </p>
    </section>
  );
}

/* -------------------------------------------------------------------- card */

export function RefusalCard({
  decision,
  checkout,
  approvedVersion,
  busy = false,
  error,
  onReview,
  onCancel,
}: {
  decision: SubmitResult;
  /** Re-read after the refusal, so the version trail and the new card are current. */
  checkout: Checkout;
  /** The version the buyer had approved and this submission tried to spend. */
  approvedVersion: number;
  busy?: boolean;
  error?: string | null;
  /**
   * Re-read the checkout. On the re-approval path that lands the buyer on the successor's
   * approval card; on every other path it lands them on whatever is actually true now.
   */
  onReview: () => void;
  onCancel?: () => void;
}) {
  // The decision is the primary evidence; the read model computes the same comparison
  // and is used only when the decision carried no field list of its own.
  const deltas = decision.deltas.length > 0 ? decision.deltas : checkout.deltas;
  // Not defaulted. `next_version` is sent on the re-approval path and nowhere else, and a
  // fallback to the current version made the card claim the refused version had been
  // superseded by itself: invalidated and "already priced and waiting" in the same breath.
  const nextVersion = decision.next_version;
  const currency =
    checkout.approval_card?.currency ??
    checkout.versions[checkout.versions.length - 1]?.currency ??
    "INR";
  const totals = totalsFrom(deltas, checkout, approvedVersion, nextVersion);
  const difference = totals ? deltaMinor(totals.approved, totals.current) : null;
  const sentence = reasonSentence(decision.explanation);
  const underWay = ALREADY_UNDER_WAY.has(decision.code);
  const liveAttempt = decision.payment_attempt_id ?? decision.attempt_id;
  // Two things license "you were not charged", not one. The code has to be on a branch
  // where the kernel creates nothing — `admit` inserts the payment attempt as its last
  // step, so every denial it returns predates one — and the checkout read back after the
  // refusal has to carry no attempt at all. The second half matters because the code table
  // only describes what *this* submission did; an attempt this submission did not make is
  // still an attempt this screen must not talk over.
  const nothingCreated =
    createdNothing(decision.code) && !underWay && checkout.attempt === null && liveAttempt === null;
  // An attempt is on the table whenever the refusal named one or the re-read found one,
  // whatever the code was. That, not the code alone, is what decides whether this card is
  // allowed to say anything about money.
  const attemptOnRecord = liveAttempt !== null || checkout.attempt !== null;

  const names: Record<string, string> = {};
  for (const line of checkout.approval_card?.quote?.lines ?? []) names[line.sku] = line.name;

  return (
    <article
      aria-live="assertive"
      aria-label="The transaction kernel refused this submission"
      className="overflow-hidden rounded-[var(--r-lg)] border border-[var(--card-line)] bg-white"
      style={{ boxShadow: "var(--card-shadow)" }}
    >
      <div className="h-1.5 w-full bg-[var(--red)]" />

      <div className="flex flex-col gap-6 px-4 py-6 sm:px-6 sm:py-7">
        <header>
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="red">Refused by the transaction kernel</Badge>
            <code className="tnum rounded-[var(--r-sm)] bg-[var(--tint-1)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--ink-3)]">
              {decision.code}
            </code>
            {codePhrase(decision.code) ? (
              <span className="text-[12px] text-[var(--ink-4)]">{codePhrase(decision.code)}</span>
            ) : null}
          </div>

          <h1 className="mt-3 max-w-[24ch] text-[20px] leading-[1.2] font-extrabold text-[var(--ink)] sm:text-[28px]">
            {headline(decision.code)}
          </h1>

          {nothingCreated ? (
            <p className="mt-2 max-w-[70ch] text-[14px] leading-[1.6] text-[var(--ink-2)]">
              <strong className="font-bold text-[var(--ink)]">You were not charged.</strong> No
              payment attempt was created and no order exists at Razorpay for this version. The
              kernel decided before anything could be spent, and refused rather than take an amount
              you had not agreed to.
            </p>
          ) : underWay ? (
            <p className="mt-2 max-w-[70ch] text-[14px] leading-[1.6] text-[var(--ink-2)]">
              <strong className="font-bold text-[var(--ink)]">
                A payment attempt for this checkout already exists.
              </strong>{" "}
              This second submission was refused so that one order cannot be paid for twice.{" "}
              <strong className="font-bold text-[var(--ink)]">
                This screen cannot tell you whether money has moved on the first attempt.
              </strong>{" "}
              A payment is captured on Razorpay&rsquo;s own evidence, so what the platform has
              recorded about that attempt is set out below, exactly as far as it goes.
            </p>
          ) : attemptOnRecord ? (
            <p className="mt-2 max-w-[70ch] text-[14px] leading-[1.6] text-[var(--ink-2)]">
              This submission was refused and created nothing of its own, but a payment attempt for
              this checkout is on record.{" "}
              <strong className="font-bold text-[var(--ink)]">
                This screen cannot tell you whether money has moved on it.
              </strong>{" "}
              What the platform has recorded about that attempt is set out below, exactly as far as
              it goes.
            </p>
          ) : (
            <p className="mt-2 max-w-[70ch] text-[14px] leading-[1.6] text-[var(--ink-2)]">
              This submission was refused with a code this storefront has no settled sentence for,
              so it does not claim on its own account whether anything was created. The kernel&rsquo;s
              own words are below, and the checkout&rsquo;s state is read from the server.
            </p>
          )}
        </header>

        {attemptOnRecord ? (
          <AttemptEvidence checkout={checkout} decidedAttemptId={liveAttempt} />
        ) : null}

        {nextVersion !== null ? (
          <section
            aria-label="The version that was refused and the version that replaces it"
            className="flex flex-wrap items-center gap-3 rounded-[var(--r-md)] border border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-3"
          >
            <span className="text-[12px] font-semibold text-[var(--ink-4)]">Version</span>
            <span className="tnum text-[18px] font-extrabold text-[var(--ink-5)] line-through decoration-[var(--red)] decoration-2">
              {approvedVersion}
            </span>
            <Arrow />
            <span className="tnum text-[18px] font-extrabold text-[var(--ink)]">{nextVersion}</span>
            <span className="text-[12px] text-[var(--ink-3)]">
              Version {approvedVersion} is permanently invalidated. Version {nextVersion} is already
              priced and waiting, with its own policy receipt and its own hold.
            </span>
          </section>
        ) : (
          <section
            aria-label="What happened to the version you approved"
            className="rounded-[var(--r-md)] border border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-3"
          >
            <p className="max-w-[70ch] text-[12px] text-[var(--ink-3)]">
              No superseding version was created. The kernel refused this submission of version{" "}
              <span className="tnum font-bold text-[var(--ink)]">{approvedVersion}</span> without
              re-pricing anything, so there is no new total to approve and nothing about this order
              has been re-quoted.
            </p>
          </section>
        )}

        {totals ? (
          <section
            aria-label="The total you approved against the total now"
            className="flex flex-wrap items-end gap-x-8 gap-y-4"
          >
            <TotalFigure caption="You approved" minor={totals.approved} currency={currency} struck />
            <Arrow />
            <TotalFigure caption="It is now" minor={totals.current} currency={currency} />
            {difference !== null && difference !== 0 ? (
              <div>
                <p className="text-[12px] font-semibold text-[var(--ink-4)]">Difference</p>
                <p
                  className={cx(
                    "tnum mt-0.5 text-[20px] font-extrabold",
                    difference > 0 ? "text-[var(--red)]" : "text-[var(--green)]",
                  )}
                >
                  {formatDelta(difference, currency)}
                </p>
              </div>
            ) : null}
          </section>
        ) : null}

        {deltas.length > 0 ? (
          <section aria-label="What changed">
            <h2 className="mb-2 text-[14px] font-bold text-[var(--ink)]">What changed</h2>
            <DeltaTable deltas={deltas} currency={currency} names={names} />
          </section>
        ) : null}

        <section
          aria-label="The reason the kernel gave"
          className="rounded-[var(--r-md)] border border-[var(--card-line)] bg-[var(--tint-3)] px-4 py-4"
        >
          <h2 className="text-[14px] font-bold text-[var(--ink)]">Why it refused</h2>
          <p className="mt-1.5 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-2)]">
            {sentence ?? "The kernel refused with a reason this storefront does not have a sentence for. It is printed below exactly as it arrived."}
          </p>
          <dl className="mt-3 flex flex-col gap-2 text-[12px] sm:flex-row sm:flex-wrap sm:gap-x-8">
            <div className="flex items-center gap-2">
              <dt className="font-semibold text-[var(--ink-4)]">Reason key</dt>
              <dd>
                <code className="rounded-[var(--r-sm)] bg-white px-1.5 py-0.5 font-mono text-[11px] break-all text-[var(--ink-2)]">
                  {decision.explanation ?? "none sent"}
                </code>
              </dd>
            </div>
            {/*
              A duplicate submission is answered by the single-winner index rather than by
              an admission, so there is no decision to name. Printing "null" beside the
              word Decision would suggest one went missing.
            */}
            {decision.decision_id ? (
              <div className="flex items-center gap-2">
                <dt className="font-semibold text-[var(--ink-4)]">Decision</dt>
                <dd>
                  <HashChip value={decision.decision_id} label="Decision id" />
                </dd>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <dt className="font-semibold text-[var(--ink-4)]">Decision</dt>
                <dd className="text-[var(--ink-3)]">
                  none — no admission ran, so no decision was taken
                </dd>
              </div>
            )}
            {decision.correlation_id ? (
              <div className="flex items-center gap-2">
                <dt className="font-semibold text-[var(--ink-4)]">Correlation</dt>
                <dd>
                  <HashChip value={decision.correlation_id} label="Correlation id" />
                </dd>
              </div>
            ) : null}
          </dl>
          <p className="mt-3 text-[12px] text-[var(--ink-4)]">
            This refusal was written to the audit log in the same transaction that produced it, so
            what was refused is evidenced as durably as what is allowed.
          </p>
        </section>

        <TrustedSurface
          tone="danger"
          label={
            nextVersion !== null
              ? "Nothing happens until you approve again. RazorAI cannot."
              : "Only you decide what happens next. RazorAI cannot."
          }
          caption={
            nextVersion !== null ? (
              <>
                This button reads the checkout again and puts version {nextVersion}&rsquo;s own
                approval card on screen. It does not approve anything. The new version has a new
                total and a new content hash, and nothing carries over from the approval you gave:
                consenting to it happens on that card, from scratch, by you.
              </>
            ) : attemptOnRecord ? (
              <>
                There is nothing new to approve. The attempt above is the one that decides this
                order, so the only useful thing this button does is read the checkout again, which
                is how the record above becomes current.
              </>
            ) : (
              <>
                There is nothing new to approve, and nothing about your approval was spent. Reading
                the checkout again is the honest next step; the kernel will answer the same way
                until the thing it refused over has changed.
              </>
            )
          }
        >
          {error ? (
            <p role="alert" className="mb-3 text-[13px] font-semibold text-[var(--red)]">
              {error}
            </p>
          ) : null}
          <TrustedActions>
            {/*
              Every branch of this button does one thing: re-read the checkout. So it is
              labelled as a read. "Review and approve version N" named an approval this
              press cannot give — the approval is a separate consent on the successor's own
              card — and "Go to the payment already in progress" promised a destination the
              re-read may not land on, since the checkout can have moved to paid, cancelled
              or unknown by the time it answers.
            */}
            <Button size="lg" onClick={onReview} busy={busy}>
              {nextVersion !== null ? `Review version ${nextVersion}` : "Read this checkout again"}
            </Button>
            {onCancel ? (
              <Button variant="ghost" size="lg" onClick={onCancel} disabled={busy}>
                Cancel this order instead
              </Button>
            ) : null}
          </TrustedActions>
        </TrustedSurface>
      </div>
    </article>
  );
}
