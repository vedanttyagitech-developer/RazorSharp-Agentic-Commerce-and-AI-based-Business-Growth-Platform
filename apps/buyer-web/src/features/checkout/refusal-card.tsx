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
 * Every figure comes from the decision the kernel returned. The one computed number on
 * the screen is a difference between two integers the server sent.
 */
"use client";

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
  payment_surface_open:
    "The payment surface for this checkout is already open, so a second submission was refused.",
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
  const nothingCreated = createdNothing(decision.code);
  const underWay = ALREADY_UNDER_WAY.has(decision.code);
  const liveAttempt = decision.payment_attempt_id ?? decision.attempt_id;

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
              This second submission was refused so that one order cannot be paid for twice. Whether
              money has moved on the first attempt is not something this screen can tell you: a
              payment is captured on Razorpay&rsquo;s own evidence, and that evidence is read below
              rather than guessed at here.
            </p>
          ) : (
            <p className="mt-2 max-w-[70ch] text-[14px] leading-[1.6] text-[var(--ink-2)]">
              This submission was refused with a code this storefront has no settled sentence for,
              so it does not claim on its own account whether anything was created. The kernel&rsquo;s
              own words are below, and the checkout&rsquo;s state is read from the server.
            </p>
          )}

          {underWay && liveAttempt ? (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span className="text-[12px] font-semibold text-[var(--ink-4)]">
                The attempt that exists
              </span>
              <HashChip value={liveAttempt} label="Payment attempt id" />
            </div>
          ) : null}
        </header>

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
                The new version has a new total and a new content hash. Nothing carries over from
                the approval you gave: reviewing version {nextVersion} means consenting to it from
                scratch, on this surface, by you.
              </>
            ) : underWay ? (
              <>
                There is nothing new to approve. The attempt that already exists is the one that
                decides this order, so the only useful thing this screen can do is read the checkout
                again and show you where that attempt has got to.
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
            <Button size="lg" onClick={onReview} busy={busy}>
              {nextVersion !== null
                ? `Review and approve version ${nextVersion}`
                : underWay
                  ? "Go to the payment already in progress"
                  : "Read this checkout again"}
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
