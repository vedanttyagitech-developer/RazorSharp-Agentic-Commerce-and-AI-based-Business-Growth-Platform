/**
 * The checkout journey: one screen, driven by the server's state, from approval to money.
 *
 * The whole journey is four presses — approve, pay, come back, wait — and the point of
 * this component is that each one is answered by the server rather than assumed by the
 * browser. It never advances its own state optimistically. Every transition here is a
 * re-read: approve returns a checkout, submit returns a decision and then a checkout,
 * paying returns nothing at all worth believing. If the network is down, the screen says
 * so; it does not draw the next step and hope.
 *
 * The branch that matters is in `submit`. `api.submitVersion` answers HTTP 200 whether
 * the kernel admitted or refused, so `allowed` is read and the status code is not. A
 * refusal is routed to `RefusalCard` and rendered as the system working.
 *
 * `cancel` answers the same way and is read the same way. A denial that is thrown away is
 * as dishonest as a denial that is thrown: the kernel refusing to abandon a checkout whose
 * payment may already be moving has to reach the buyer as words, not as a screen that
 * blinks and changes nothing.
 *
 * Idempotency keys are held per logical action rather than per click. A retry after a
 * dropped connection must replay the first answer, not run a second admission, and the
 * key is what tells the server those two requests are the same intention.
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Amount, Badge, Button, Card, ErrorState, Skeleton, cx } from "@/components/ui";
import { api, newIdempotencyKey } from "@/lib/api/client";
import { humanMessage } from "@/lib/api/problem";
import type { Checkout, SubmitResult } from "@/lib/api/types";

import { ApprovalCard, HashChip } from "./approval-card";
import { DeltaTable } from "./delta-table";
import { PaymentPanel } from "./payment-panel";
import { RefusalCard, codePhrase, reasonSentence } from "./refusal-card";
import { StateBanner, isTerminalState, stateMeaning } from "./state-banner";
import { TrustedActions, TrustedSurface } from "./trusted-surface";

/**
 * States where the payment surface, not the approval surface, is the right screen.
 *
 * INVALIDATED_AWAITING_PAYMENT_RESULT is in this set even though no payment may be made
 * from it. The panel is the only thing on this route that keeps re-reading the checkout,
 * and this is the state that most needs re-reading: it ends when the provider answers,
 * not when the buyer does something. The panel disables its own Pay button for it.
 */
const PAYING = new Set([
  "EXECUTION_PENDING",
  "AWAITING_PAYMENT",
  "PAYMENT_UNKNOWN",
  "INVALIDATED_AWAITING_PAYMENT_RESULT",
]);

type Busy = "approve" | "reject" | "submit" | "cancel" | null;

/**
 * A cancellation the kernel would not perform, held as the three fields it sent.
 *
 * `from_state` is the kernel's own reading of where the checkout was when it refused, and
 * it is kept rather than paraphrased: it is the difference between telling the buyer the
 * state their cancellation broke against and telling them a general reassurance.
 */
interface CancelRefusal {
  code: string;
  explanation: string | null;
  fromState: string | null;
}

/** `2026-09-05T10:23:44.512Z` rendered without a locale, so the server and client agree. */
function plainInstant(iso: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/.exec(iso);
  return match ? `${match[1]} ${match[2]} UTC` : iso;
}

function VersionTrail({ checkout }: { checkout: Checkout }) {
  if (checkout.versions.length === 0) return null;
  return (
    <section aria-label="Every version of this checkout" className="mt-2">
      <h2 className="mb-2 text-[13px] font-bold text-[var(--ink-2)]">Version trail</h2>
      <p className="mb-3 max-w-[70ch] text-[12px] text-[var(--ink-4)]">
        Superseded versions are kept and shown. They are the evidence that an old approval was
        refused, and hiding them would leave this screen unable to say what changed.
      </p>
      <ol className="flex flex-col gap-2">
        {checkout.versions.map((version) => {
          const live = version.version === checkout.current_version;
          return (
            <li
              key={version.version}
              className={cx(
                "flex flex-wrap items-center gap-x-4 gap-y-1 rounded-[var(--r-md)] border px-3 py-2",
                live ? "border-[var(--ink)] bg-white" : "border-[var(--card-line)] bg-[var(--tint-3)]",
              )}
            >
              <span className="tnum text-[13px] font-extrabold text-[var(--ink)]">v{version.version}</span>
              <code className="rounded-[var(--r-sm)] bg-[var(--tint-1)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--ink-3)]">
                {version.state}
              </code>
              <Amount
                minor={version.amount_minor}
                currency={version.currency}
                className="text-[13px] font-semibold text-[var(--ink-2)]"
              />
              <HashChip value={version.content_hash} label={`Version ${version.version} content hash`} />
              {version.approval ? (
                <Badge tone="green">approved {plainInstant(version.approval.approved_at)}</Badge>
              ) : null}
              <span className="tnum ml-auto text-[11px] text-[var(--ink-5)]">
                {plainInstant(version.created_at)}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/**
 * How hard a recorded decision tries to see itself reflected before saying so out loud.
 *
 * Bounded deliberately. The API has no measured read-after-write gap here, so a long
 * poll would be treating a symptom nobody has explained; a few short attempts cover a
 * transient and then the buyer is told the truth rather than left on a live button.
 */
const DECISION_CONFIRM_ATTEMPTS = 5;
const DECISION_CONFIRM_DELAY_MS = 400;

/**
 * What the buyer is told when the decision was accepted and the screen cannot yet see it.
 *
 * It says the decision is recorded, because the server accepted it and that is a fact.
 * It does not say approved or declined, because this screen has not read which. And it
 * asks them not to press again, since a second press would be a second consent.
 */
const DECISION_RECORDED_UNSEEN =
  "Your decision was recorded, but this page has not been able to read it back yet. " +
  "Do not decide again \u2014 refresh in a moment, and if it still asks, the order page " +
  "will show what the platform actually holds.";

export function CheckoutJourney({ checkoutId }: { checkoutId: string }) {
  const [checkout, setCheckout] = useState<Checkout | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<{ decision: SubmitResult; approvedVersion: number } | null>(null);
  /** The kernel's verdict on a cancellation it would not perform. Null when it performed one. */
  const [cancelVerdict, setCancelVerdict] = useState<CancelRefusal | null>(null);

  /**
   * One key per logical action, minted on the first attempt and kept until it succeeds.
   * A retried approve must replay the recorded approval, not record a second one.
   */
  const keys = useRef(new Map<string, string>());
  const keyFor = useCallback((action: string) => {
    const existing = keys.current.get(action);
    if (existing) return existing;
    const fresh = newIdempotencyKey();
    keys.current.set(action, fresh);
    return fresh;
  }, []);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const next = await api.checkout(checkoutId, signal);
        setCheckout(next);
        setLoadError(null);
        return next;
      } catch (cause) {
        if (cause instanceof DOMException && cause.name === "AbortError") return null;
        setLoadError(humanMessage(cause));
        return null;
      }
    },
    [checkoutId],
  );

  useEffect(() => {
    const controller = new AbortController();
  // The lint rule cannot see past an `await`: the state this sets is set in the promise's
  // continuation, not synchronously in the effect body, and reading a checkout the moment
  // its route mounts is precisely the "subscribe to an external system" case the rule's own
  // guidance permits. There is no render-time source for a payment's state to derive from.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  /**
   * Consent, echoing back the exact card that was on screen.
   *
   * `api.approve` takes the whole card because the request must repeat the content hash,
   * amount and currency the buyer actually saw; a card that moved underneath them is
   * refused by the server rather than approved on their behalf. The result is an approval
   * record, not a checkout, so the checkout is re-read rather than assumed.
   */
  /**
   * Re-read until the platform stops asking for a decision this buyer has already made.
   *
   * A successful approve or reject returns a decision record, not a checkout, so the
   * screen has to re-read to learn what it now is. That re-read used to happen once, and
   * once is not enough: a peer session caught a live run where the POST succeeded, the
   * server held the checkout APPROVED at version 1, and the card was still on screen
   * asking for the same approval, with nothing scheduled to look again.
   *
   * That is worse than a stale screen. The idempotency key is deleted on success, so a
   * second press mints a fresh one and sends a genuinely new approve against a version
   * that already carries consent -- the storefront inviting a buyer to consent twice to
   * one thing, on the one path where consent is the product.
   *
   * The cause is not a read-after-write gap at the API: 53 approve-then-read cycles driven
   * straight at it came back APPROVED on the immediate read every time. So this is bounded
   * rather than generous -- a few short attempts, and then an honest sentence. It never
   * assumes the decision landed on the strength of the POST alone; the server stays the
   * only thing that says what a checkout is.
   */
  const confirmDecided = useCallback(
    async (version: number) => {
      for (let attempt = 0; attempt < DECISION_CONFIRM_ATTEMPTS; attempt += 1) {
        const next = await load();
        if (next === null) return false;
        const stillAsking =
          next.state === "APPROVAL_REQUIRED" && next.approval_card?.version === version;
        if (!stillAsking) return true;
        await new Promise((resolve) => setTimeout(resolve, DECISION_CONFIRM_DELAY_MS));
      }
      return false;
    },
    [load],
  );

  const approve = useCallback(async () => {
    const card = checkout?.approval_card;
    if (!card) return;
    setBusy("approve");
    setActionError(null);
    setCancelVerdict(null);
    try {
      await api.approve(card, keyFor(`approve:${card.version}:${card.content_hash}`));
      keys.current.delete(`approve:${card.version}:${card.content_hash}`);
      if (!(await confirmDecided(card.version))) setActionError(DECISION_RECORDED_UNSEEN);
    } catch (cause) {
      setActionError(humanMessage(cause));
    } finally {
      setBusy(null);
    }
  }, [checkout, confirmDecided, keyFor]);

  const reject = useCallback(async () => {
    const card = checkout?.approval_card;
    if (!card) return;
    setBusy("reject");
    setActionError(null);
    setCancelVerdict(null);
    try {
      await api.reject(card, "buyer_declined", keyFor(`reject:${card.version}:${card.content_hash}`));
      keys.current.delete(`reject:${card.version}:${card.content_hash}`);
      if (!(await confirmDecided(card.version))) setActionError(DECISION_RECORDED_UNSEEN);
    } catch (cause) {
      setActionError(humanMessage(cause));
    } finally {
      setBusy(null);
    }
  }, [checkout, confirmDecided, keyFor]);

  /**
   * End the checkout, and read the kernel's answer instead of assuming it agreed.
   *
   * `POST .../cancel` is HTTP 200 whether it cancelled or refused, exactly like submit, and
   * `allowed` is the only field that says which. Discarding the return value made a refused
   * cancellation — the kernel declining to abandon a checkout whose payment may already be
   * moving — look identical to a successful one: the refusal card was wiped, the state
   * re-rendered unchanged, and nothing on screen said a word about it.
   *
   * The key is spent either way. A refusal is a completed operation with a definite answer,
   * so a second, later press is a second intention and deserves an admission of its own
   * rather than a replay of the first verdict.
   */
  const cancel = useCallback(async () => {
    setBusy("cancel");
    setActionError(null);
    setCancelVerdict(null);
    try {
      const outcome = await api.cancel(checkoutId, "buyer_cancelled", keyFor("cancel"));
      keys.current.delete("cancel");
      if (outcome.allowed === false) {
        setCancelVerdict({
          code: outcome.code ?? "",
          explanation: outcome.explanation ?? null,
          fromState: outcome.from_state ?? null,
        });
        await load();
        return;
      }
      setRefusal(null);
      await load();
    } catch (cause) {
      setActionError(humanMessage(cause));
    } finally {
      setBusy(null);
    }
  }, [checkoutId, keyFor, load]);

  /**
   * Ask the kernel to admit the approval.
   *
   * The two outcomes are both 200 and both normal. `allowed` decides which screen the
   * buyer sees next; the HTTP status decides nothing at all.
   */
  const submit = useCallback(async () => {
    if (!checkout) return;
    const version = checkout.current_version;
    setBusy("submit");
    setActionError(null);
    setCancelVerdict(null);
    try {
      const decision = await api.submitVersion(checkoutId, version, keyFor(`submit:${version}`));
      // The key is spent either way. An admitted version has its one attempt, and a
      // refused version can never be admitted at all, so the next submission is of a
      // different version and deserves a key of its own.
      keys.current.delete(`submit:${version}`);
      const fresh = await load();
      setRefusal(decision.allowed ? null : { decision, approvedVersion: version });
      if (!fresh) setActionError("The decision was recorded but this page could not re-read the checkout.");
    } catch (cause) {
      setActionError(humanMessage(cause));
    } finally {
      setBusy(null);
    }
  }, [checkout, checkoutId, keyFor, load]);

  const reviewNewVersion = useCallback(async () => {
    setBusy("submit");
    setActionError(null);
    setCancelVerdict(null);
    const fresh = await load();
    if (fresh) setRefusal(null);
    setBusy(null);
  }, [load]);

  /* ---------------------------------------------------------------- render --- */

  if (!checkout && loadError) {
    return (
      <Card className="mt-6">
        <ErrorState title="This checkout could not be read" detail={loadError} onRetry={() => void load()} />
      </Card>
    );
  }

  if (!checkout) {
    return (
      <div className="mt-6 flex flex-col gap-4" aria-busy="true" aria-label="Loading this checkout">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-56 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  const meaning = stateMeaning(checkout.state);

  return (
    <div className="flex flex-col gap-6 pb-16">
      <header className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b border-[var(--header-line)] pb-4">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-[18px] font-extrabold text-[var(--ink)]">Checkout</h1>
          <code className="font-mono text-[12px] break-all text-[var(--ink-4)]">{checkout.checkout_id}</code>
        </div>
        <p className="tnum text-[12px] text-[var(--ink-5)]">
          version {checkout.current_version} · {meaning.title} · read at{" "}
          {plainInstant(checkout.updated_at)}
        </p>
      </header>

      {loadError ? (
        <p role="alert" className="rounded-[var(--r-md)] border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-[var(--red)]">
          The last refresh failed: {loadError} The information below is the last good read, not
          necessarily what is true now.
        </p>
      ) : null}

      {cancelVerdict ? <CancelVerdict verdict={cancelVerdict} /> : null}

      {refusal ? (
        <RefusalCard
          decision={refusal.decision}
          checkout={checkout}
          approvedVersion={refusal.approvedVersion}
          busy={busy === "submit"}
          error={actionError}
          onReview={() => void reviewNewVersion()}
          onCancel={checkout.cancellable ? () => void cancel() : undefined}
        />
      ) : checkout.state === "APPROVAL_REQUIRED" && checkout.approval_card ? (
        <ApprovalCard
          card={checkout.approval_card}
          busy={busy === "approve" ? "approve" : busy === "reject" ? "reject" : null}
          error={actionError}
          onApprove={() => void approve()}
          onReject={() => void reject()}
        />
      ) : checkout.state === "APPROVED" ? (
        <div className="flex flex-col gap-5">
          <StateBanner state={checkout.state} />
          <Card className="px-4 py-5 sm:px-5">
            <h2 className="text-[16px] font-extrabold text-[var(--ink)]">
              Version {checkout.current_version} is approved
            </h2>
            <p className="mt-1 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-3)]">
              Pressing Pay asks the transaction kernel to spend that approval. It will compare the
              bytes you approved against what the merchant is selling right now. If they match, it
              issues a single execution grant and a payment order is created. If they do not, it
              refuses, tells you exactly what moved, and charges you nothing.
            </p>
          </Card>
          <TrustedSurface
            label="You are paying this. RazorAI cannot."
            caption="Submitting is the moment the kernel decides. Either outcome leaves you informed and neither one charges you a figure you did not agree to."
          >
            {actionError ? (
              <p role="alert" className="mb-3 text-[13px] font-semibold text-[var(--red)]">
                {actionError}
              </p>
            ) : null}
            <TrustedActions>
              <Button size="lg" onClick={() => void submit()} busy={busy === "submit"} disabled={busy !== null}>
                Pay
              </Button>
              {checkout.cancellable ? (
                <Button variant="ghost" size="lg" onClick={() => void cancel()} busy={busy === "cancel"} disabled={busy !== null}>
                  Cancel this order
                </Button>
              ) : null}
            </TrustedActions>
          </TrustedSurface>
        </div>
      ) : PAYING.has(checkout.state) ? (
        <PaymentPanel checkout={checkout} onCheckout={setCheckout} />
      ) : checkout.state === "PAID" ? (
        <div className="flex flex-col gap-5">
          <StateBanner state={checkout.state} />
          <Card className="px-4 py-5 sm:px-5">
            <h2 className="text-[16px] font-extrabold text-[var(--ink)]">Paid</h2>
            <p className="mt-1 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-3)]">
              The order was written against version {checkout.current_version}, the version you
              approved, on evidence from Razorpay rather than from this browser.
            </p>
            {checkout.order_id ? (
              <p className="mt-3 text-[13px]">
                <a
                  href={`/orders/${encodeURIComponent(checkout.order_id)}`}
                  className="font-semibold text-[var(--green)] underline underline-offset-2"
                >
                  See the order and its evidence
                </a>
                <code className="ml-2 font-mono text-[11px] break-all text-[var(--ink-5)]">
                  {checkout.order_id}
                </code>
              </p>
            ) : null}
            {checkout.attempt?.capture_evidence ? (
              <p className="mt-2 text-[12px] text-[var(--ink-4)]">
                Capture evidence: {checkout.attempt.capture_evidence.kind}, reference{" "}
                <code className="font-mono text-[11px] break-all">
                  {checkout.attempt.capture_evidence.reference}
                </code>
                , verified {plainInstant(checkout.attempt.capture_evidence.verified_at)}.
              </p>
            ) : null}
          </Card>
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          <StateBanner state={checkout.state} />
          {checkout.state === "APPROVAL_REQUIRED" ? (
            <Card className="px-4 py-5">
              <p className="max-w-[70ch] text-[13px] text-[var(--ink-3)]">
                This checkout is waiting for an approval but the server did not send an approval
                card with it, so there are no bytes to show you and nothing to consent to. Reload
                to read it again.
              </p>
            </Card>
          ) : null}
          {checkout.deltas.length > 0 ? (
            <Card className="px-4 py-5">
              <h2 className="mb-2 text-[14px] font-bold text-[var(--ink)]">
                What changed on the way here
              </h2>
              <p className="mb-3 text-[13px] text-[var(--ink-3)]">
                The read model computed these differences between the last two versions.
              </p>
              <RefusalDeltasOnly checkout={checkout} />
            </Card>
          ) : null}
          {!isTerminalState(checkout.state) || checkout.cancellable ? (
            <TrustedSurface label="Only you can end this. RazorAI cannot.">
              {actionError ? (
                <p role="alert" className="mb-3 text-[13px] font-semibold text-[var(--red)]">
                  {actionError}
                </p>
              ) : null}
              <TrustedActions>
                <Button variant="ghost" size="md" onClick={() => void load()} disabled={busy !== null}>
                  Read this checkout again
                </Button>
                {checkout.cancellable ? (
                  <Button
                    variant="danger"
                    size="md"
                    onClick={() => void cancel()}
                    busy={busy === "cancel"}
                    disabled={busy !== null}
                  >
                    Cancel this order
                  </Button>
                ) : null}
              </TrustedActions>
            </TrustedSurface>
          ) : null}
        </div>
      )}

      <VersionTrail checkout={checkout} />
    </div>
  );
}

/**
 * A cancellation the kernel would not perform, said in the kernel's own terms.
 *
 * It uses the same reason and code vocabulary the refusal card owns, because a buyer who
 * has just been told "a payment attempt for this checkout is already in flight" on one
 * screen should read the same sentence when the same fact refuses their cancellation.
 * `aria-live="assertive"` because the buyer pressed a button expecting the order to end.
 */
function CancelVerdict({ verdict }: { verdict: CancelRefusal }) {
  const sentence = reasonSentence(verdict.explanation);
  const phrase = codePhrase(verdict.code);
  return (
    <section
      role="status"
      aria-live="assertive"
      aria-label="Your cancellation was refused"
      className="rounded-[var(--r-md)] border-2 border-[var(--red)] bg-white px-4 py-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="red">Cancellation refused</Badge>
        <code className="rounded-[var(--r-sm)] bg-[var(--tint-1)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--ink-3)]">
          {verdict.code || "no code sent"}
        </code>
        {phrase ? <span className="text-[12px] text-[var(--ink-4)]">{phrase}</span> : null}
      </div>
      <p className="mt-2 max-w-[70ch] text-[13px] leading-[1.55] text-[var(--ink-2)]">
        {sentence ??
          "The kernel would not cancel this checkout and gave a reason this storefront has no sentence for. It is printed above exactly as it arrived."}{" "}
        {/*
          The refusal says the cancellation did not happen. It does not say that nothing at
          all happened, which is a wider claim and one this response cannot carry: a worker
          or a webhook may have moved this checkout in the same seconds. So the sentence is
          held to the cancellation, and the state is read from the server rather than
          asserted here.
        */}
        This checkout was not cancelled.{" "}
        {verdict.fromState ? (
          <>
            The kernel read it as{" "}
            <code className="rounded-[var(--r-sm)] bg-[var(--tint-1)] px-1 py-0.5 font-mono text-[11px]">
              {verdict.fromState}
            </code>{" "}
            when it refused; where it stands now is read from the server below.
          </>
        ) : (
          <>Where it stands now is read from the server below.</>
        )}
      </p>
    </section>
  );
}

/**
 * The read model's deltas outside a refusal.
 *
 * Split out rather than inlined so the terminal branch stays readable; it renders the
 * same table the refusal does, over `checkout.deltas` rather than a decision's.
 */
function RefusalDeltasOnly({ checkout }: { checkout: Checkout }) {
  const currency =
    checkout.approval_card?.currency ??
    checkout.versions[checkout.versions.length - 1]?.currency ??
    "INR";
  const names: Record<string, string> = {};
  for (const line of checkout.approval_card?.quote?.lines ?? []) names[line.sku] = line.name;
  return <DeltaTable deltas={checkout.deltas} currency={currency} names={names} />;
}
