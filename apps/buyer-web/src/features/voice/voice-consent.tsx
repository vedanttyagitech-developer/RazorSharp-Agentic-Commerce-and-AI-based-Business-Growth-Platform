/**
 * Voice as a second way to press the Approve button. The button itself is untouched.
 *
 * What happens, in order. The buyer presses "Or say it aloud", which is the user gesture
 * the microphone needs. This component opens a voice session and, once the gateway is
 * ready, sends `read_card` naming the checkout and version on screen -- and nothing else,
 * because a hash or an amount supplied from here would be a consent surface this page
 * authored. The gateway reads the card from the trusted server with the buyer's own
 * bearer, prints the reading before it speaks it, speaks the version and the amount in
 * digits and words, and opens a window for a spoken yes or no when it has finished
 * SENDING the audio. The microphone is opened for exactly that window and closed after.
 *
 * When `consent_recognised` arrives, this component compares the five fields the gateway
 * READ against the five fields of the card this page is DISPLAYING -- checkout, version,
 * hash, amount, currency -- and only if all five agree does it call `onApprove`, which is
 * the button's own callback. So the request that reaches the kernel is the button's
 * request, built from the card on screen, and the kernel compares those bytes to the
 * version it holds exactly as it would for a press. Three copies of the hash have to agree
 * before an approval exists: read, shown, stored.
 *
 * Every other outcome is a sentence beside the button, never silence: a no, a near miss,
 * a window that lapsed, a reading that was spoken over, a card that moved, a service that
 * could not read the card. The buyer sees both ways to consent and sees which one fired.
 *
 * What this component cannot do is as deliberate as what it can: it sends no approve
 * request of its own, it never rejects (a misheard no would release the reservation), and
 * it stops at approval. Paying is a separate press on a separate surface.
 */
"use client";

import { useEffect, useRef, useState } from "react";

import { cx } from "@/components/ui";
import type { ApprovalCard } from "@/lib/api/types";
import { formatMinor } from "@/lib/money";

import type { ConsentState, TranscriptEntry } from "./transcript";
import { useVoiceSession, type UseVoiceSessionOptions } from "./use-voice-session";
import type { ConsentRecognised } from "./wire";

export type ConsentLocale = "en-IN" | "hi-IN";

export interface VoiceConsentProps extends UseVoiceSessionOptions {
  /** The card on screen: the only card a spoken yes may ever be matched against. */
  card: ApprovalCard;
  /** The button's own busy state, so a recognised yes never doubles a press in flight. */
  busy: "approve" | "reject" | null;
  /** The button's own callback. Voice calls it; it never builds a request of its own. */
  onApprove: () => void;
  locale?: ConsentLocale;
  className?: string;
}

/**
 * Why a recognised yes does not match the card on screen, or null when it does.
 *
 * Every field is compared, and the sentence names both sides. A buyer who consented to
 * version 1 while the screen moved to version 2 should read exactly that.
 */
export function describeMismatch(frame: ConsentRecognised, card: ApprovalCard): string | null {
  if (frame.checkout_id !== card.checkout_id) {
    return "The reading was of a different checkout from the one on this screen.";
  }
  if (frame.version !== card.version) {
    return `You heard version ${frame.version}; this screen shows version ${card.version}.`;
  }
  if (frame.content_hash !== card.content_hash) {
    return (
      `You heard version ${frame.version} with hash ${frame.content_hash.slice(0, 12)}…; ` +
      `this screen shows hash ${card.content_hash.slice(0, 12)}….`
    );
  }
  if (frame.amount_minor !== card.amount_minor || frame.currency !== card.currency) {
    return (
      `You heard ${formatMinor(frame.amount_minor, frame.currency)}; ` +
      `this screen shows ${formatMinor(card.amount_minor, card.currency)}.`
    );
  }
  return null;
}

/** What this component did with a recognised yes. Keyed by consent id, decided once. */
type Decision = { kind: "sent" } | { kind: "mismatch"; detail: string } | { kind: "busy" };

/** The reading's own text, as the gateway printed it before speaking (text before speech). */
function readingText(entries: readonly TranscriptEntry[], consent: ConsentState): string | null {
  if (!consent.card) return null;
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (
      entry.kind === "assistant" &&
      entry.deterministic &&
      entry.templateId === consent.card.template_id &&
      entry.speechGeneration === consent.card.speech_generation
    ) {
      return entry.text;
    }
  }
  return null;
}

/**
 * Seconds left in the window, counted down from the moment it opened on this page.
 *
 * The reading is keyed by the window's id, so a count left over from a previous window
 * reads as null rather than as the new window's time. The first tick is scheduled, not
 * taken inline, for the same reason `approval-card.tsx` schedules its clock: state is
 * only ever set from the timer's callback.
 */
function useCountdown(seconds: number | null, key: string | null): number | null {
  const [count, setCount] = useState<{ key: string; left: number } | null>(null);
  useEffect(() => {
    if (seconds === null || key === null) return;
    const startedAt = Date.now();
    const tick = () =>
      setCount({ key, left: Math.max(0, Math.ceil(seconds - (Date.now() - startedAt) / 1000)) });
    const first = window.setTimeout(tick, 0);
    const timer = window.setInterval(tick, 250);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [seconds, key]);
  return count !== null && count.key === key ? count.left : null;
}

export function VoiceConsent({
  card,
  busy,
  onApprove,
  locale = "en-IN",
  className,
  ...sessionOptions
}: VoiceConsentProps) {
  const voice = useVoiceSession(sessionOptions);
  const { connection, transcript } = voice;
  const consent = transcript.consent;
  const running = connection !== "idle" && connection !== "closed";
  const ready = connection === "open" && transcript.ready !== null;

  /**
   * A reading the buyer asked for before the socket was open. A ref, not state: nothing
   * on screen depends on it -- "connecting" is read off the session -- and it is consumed
   * by the effect below the moment the gateway is ready.
   */
  const pendingRead = useRef(false);
  /** What was decided about each recognised consent id. A frame replayed by page script
   *  finds its id here and does nothing; the kernel would refuse a second approval too. */
  const decisions = useRef(new Map<string, Decision>());
  const [decided, setDecided] = useState<{ consentId: string; decision: Decision } | null>(null);

  const askForReading = () => {
    if (ready) {
      // The frame names the card on screen and nothing about its contents.
      voice.readCard(card.checkout_id, card.version, locale);
      return;
    }
    pendingRead.current = true;
    if (!running) voice.start();
  };

  useEffect(() => {
    if (!ready || !pendingRead.current) return;
    pendingRead.current = false;
    voice.readCard(card.checkout_id, card.version, locale);
  }, [ready, voice, card.checkout_id, card.version, locale]);

  // The microphone carries real audio only while the window is open. Outside it the
  // session keeps sending silence, as the recognizer contract requires, and a stray
  // remark cannot become a turn.
  const listening = consent.status === "listening";
  useEffect(() => {
    voice.setTransmitting(listening);
  }, [listening, voice]);

  const recognised = consent.status === "recognised" ? consent.recognised : null;
  useEffect(() => {
    if (!recognised) return;
    if (decisions.current.has(recognised.consent_id)) return;
    const mismatch = describeMismatch(recognised, card);
    const decision: Decision =
      mismatch !== null ? { kind: "mismatch", detail: mismatch } : busy !== null ? { kind: "busy" } : { kind: "sent" };
    decisions.current.set(recognised.consent_id, decision);
    // The frame arrived from a socket, outside React, and this is the one place the
    // page learns what was done with it; the sentence beside the button is drawn from it.
    setDecided({ consentId: recognised.consent_id, decision });
    if (decision.kind === "sent") onApprove();
  }, [recognised, card, busy, onApprove]);

  const countdown = useCountdown(listening ? consent.closesInS : null, listening ? consent.consentId : null);
  const reading = readingText(transcript.entries, consent);
  const unavailable = transcript.degradations.find(
    (notice) => notice.kind === "card_unavailable" || notice.kind === "stt_unavailable",
  );
  const decision = decided && decided.consentId === consent.consentId ? decided.decision : null;
  const shownAmount = formatMinor(card.amount_minor, card.currency);

  return (
    <div
      className={cx("mt-4 border-t border-[var(--card-line)] pt-4", className)}
      aria-label="Approve by voice"
    >
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={askForReading}
          disabled={listening || (running && !ready) || busy !== null}
          className="inline-flex items-center gap-2 rounded-[var(--r-md)] border border-[var(--ink)] bg-white px-3 py-2 text-[13px] font-semibold text-[var(--ink)] transition hover:bg-[var(--tint-2)] disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--blue)]"
        >
          <MicGlyph />
          {consent.status === "idle" && !running ? "Or say it aloud" : "Read it again"}
        </button>
        <p className="max-w-[60ch] text-[12px] leading-[1.45] text-[var(--ink-4)]">
          RazorAI reads version {card.version} and {shownAmount} to you. A spoken yes presses
          Approve for you; a spoken no records nothing. It never pays.
        </p>
      </div>

      <div role="status" aria-live="polite" className="mt-3 flex flex-col gap-1.5 text-[13px] leading-[1.5]">
        {unavailable ? (
          <Line tone="warn">
            Voice approval is unavailable: {unavailable.message} The button works.
          </Line>
        ) : null}

        {running && !ready ? <Line tone="muted">Connecting the microphone…</Line> : null}

        {reading ? (
          <Line tone="ink">
            <span className="font-semibold">Reading:</span> {reading}
          </Line>
        ) : null}

        {listening ? (
          <Line tone="ink">
            <span className="font-semibold">Listening for yes or no</span>
            {countdown !== null ? <span className="tnum"> — {countdown}s</span> : null}
          </Line>
        ) : null}

        {consent.nearMiss && (listening || consent.status === "closed") ? (
          <Line tone="muted">
            {consent.nearMiss.reason === "began_before_reading_ended"
              ? `You started speaking before the amount was read (“${consent.nearMiss.text}”), so it does not count. Say it again.`
              : `Heard “${consent.nearMiss.text}” — not a yes or a no. Nothing was recorded.`}
          </Line>
        ) : null}

        {consent.status === "recognised" && recognised ? (
          decision?.kind === "sent" ? (
            <Line tone="ok">
              Heard “{recognised.heard}” for version {recognised.version},{" "}
              {formatMinor(recognised.amount_minor, recognised.currency)} — sending your approval
              exactly as the button would.
            </Line>
          ) : decision?.kind === "mismatch" ? (
            <Line tone="warn">
              Heard “{recognised.heard}”, but it does not match this screen: {decision.detail} Nothing
              was sent.
            </Line>
          ) : decision?.kind === "busy" ? (
            <Line tone="warn">
              Heard “{recognised.heard}”, but another action was already in flight. Nothing was sent;
              press the button when it finishes.
            </Line>
          ) : null
        ) : null}

        {consent.status === "declined" ? (
          <Line tone="ink">
            You said “{consent.heard}”. Nothing was recorded. This version is still open; use Reject
            to release it.
          </Line>
        ) : null}

        {consent.status === "closed" && consent.closedReason === "expired" ? (
          <Line tone="muted">
            No clear yes or no in time. Nothing was recorded. Press the button, or read it again.
          </Line>
        ) : null}
        {consent.status === "closed" && consent.closedReason === "barge_in" ? (
          <Line tone="muted">
            You spoke over the reading, so it does not count. Nothing was recorded. Read it again.
          </Line>
        ) : null}
      </div>
    </div>
  );
}

function Line({ tone, children }: { tone: "ink" | "muted" | "warn" | "ok"; children: React.ReactNode }) {
  const colour =
    tone === "warn"
      ? "text-[var(--red)]"
      : tone === "ok"
        ? "text-[var(--green)]"
        : tone === "muted"
          ? "text-[var(--ink-4)]"
          : "text-[var(--ink-2)]";
  return <p className={cx("max-w-[70ch]", colour)}>{children}</p>;
}

function MicGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
      <rect x="5.5" y="1.5" width="5" height="8" rx="2.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.5M5.5 14.5h5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}
